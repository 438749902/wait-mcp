#!/usr/bin/env python3
"""Small stdio MCP server for durable, blocking shell jobs."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any


HOME = Path(os.environ.get("WAIT_MCP_HOME", Path.home() / ".codex-wait-mcp"))
LOGS = HOME / "logs"
DB_PATH = HOME / "jobs.sqlite"
TAIL_LINES = 20
MAX_OUTPUT_BYTES = 1024 * 1024


class RequestCancelled(Exception):
    """The MCP client cancelled an in-flight request."""


def now() -> float:
    return time.time()


def iso(ts: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


def tail(path: str, lines: int = TAIL_LINES) -> list[str]:
    if not path or lines <= 0:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            data = b""
            position = f.tell()
            while position and data.count(b"\n") <= lines:
                size = min(8192, position)
                position -= size
                f.seek(position)
                data = f.read(size) + data
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-lines:]


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok and code.value == 259)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def recovered_exit_code(pid: int) -> int | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    code = ctypes.c_ulong()
    ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return int(code.value) if ok else None


TOOLS = [
    {"name": "run", "description": "Start a shell job and return immediately with a durable job_id.", "inputSchema": {"type": "object", "required": ["command"], "properties": {"command": {"description": "Shell command string or argv array.", "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}, "cwd": {"type": "string"}, "env": {"type": "object", "additionalProperties": {"type": "string"}}, "name": {"type": "string"}}}},
    {"name": "wait", "description": "Block inside the MCP call until one or all target jobs finish. Never polls the caller. Supports MCP request cancellation.", "inputSchema": {"type": "object", "properties": {"job_ids": {"type": "array", "items": {"type": "string"}}, "mode": {"type": "string", "enum": ["all", "any"], "default": "all"}}}},
    {"name": "output", "description": "Read current output without waiting for completion.", "inputSchema": {"type": "object", "required": ["job_id"], "properties": {"job_id": {"type": "string"}, "tail_lines": {"type": "integer", "minimum": 1}, "offset": {"type": "object", "properties": {"stdout": {"type": "integer", "minimum": 0}, "stderr": {"type": "integer", "minimum": 0}}}}}},
    {"name": "kill", "description": "Gracefully terminate a job, then kill its process tree after a short timeout.", "inputSchema": {"type": "object", "required": ["job_id"], "properties": {"job_id": {"type": "string"}, "timeout_sec": {"type": "number", "minimum": 0, "default": 5}}}},
    {"name": "list", "description": "List durable jobs by status, optionally filtered by working directory.", "inputSchema": {"type": "object", "properties": {"status": {"type": "string", "enum": ["running", "completed", "failed", "killed"]}, "cwd": {"type": "string", "description": "Only return jobs whose normalized working directory matches this path."}}}},
]


class JobStore:
    def __init__(self) -> None:
        HOME.mkdir(parents=True, exist_ok=True)
        LOGS.mkdir(exist_ok=True)
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.done = threading.Condition()
        self.jobs: dict[str, dict[str, Any]] = {}
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, name TEXT, command TEXT NOT NULL, cwd TEXT NOT NULL,
                pid INTEGER NOT NULL, start_time REAL NOT NULL, end_time REAL,
                status TEXT NOT NULL, exit_code INTEGER, stdout_path TEXT NOT NULL, stderr_path TEXT NOT NULL
            )""")
            self.db.commit()
        self.recover()

    def row(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def save(self, job: dict[str, Any]) -> None:
        with self.lock:
            self.db.execute("""INSERT OR REPLACE INTO jobs
                (job_id,name,command,cwd,pid,start_time,end_time,status,exit_code,stdout_path,stderr_path)
                VALUES (:job_id,:name,:command,:cwd,:pid,:start_time,:end_time,:status,:exit_code,:stdout_path,:stderr_path)""", job)
            self.db.commit()

    def jobs_for(self, status: str | None = None, cwd: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            query = "SELECT * FROM jobs" + (" WHERE status=?" if status else "") + " ORDER BY start_time"
            rows = self.db.execute(query, ((status,) if status else ())).fetchall()
        jobs = [dict(r) for r in rows]
        if cwd is None:
            return jobs
        if not isinstance(cwd, str):
            raise ValueError("cwd must be a string")
        wanted = os.path.normcase(os.path.normpath(str(Path(cwd).expanduser().resolve())))
        return [j for j in jobs if os.path.normcase(os.path.normpath(j["cwd"])) == wanted]

    def recover(self) -> None:
        for job in self.jobs_for("running"):
            if pid_alive(job["pid"]):
                self.jobs[job["job_id"]] = job
                threading.Thread(target=self.recover_watch, args=(job,), daemon=True).start()
            else:
                job.update(status="failed", end_time=now(), exit_code=recovered_exit_code(job["pid"]))
                self.save(job)

    def recover_watch(self, job: dict[str, Any]) -> None:
        code = None
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, job["pid"])
            if handle:
                kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
                code = ctypes.c_ulong()
                kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                code = int(code.value)
                kernel32.CloseHandle(handle)
        else:
            while pid_alive(job["pid"]):
                time.sleep(1)
            code = recovered_exit_code(job["pid"])
        job.update(status="completed" if code == 0 else "failed", end_time=now(), exit_code=code)
        self.save(job)
        self.jobs.pop(job["job_id"], None)
        with self.done:
            self.done.notify_all()

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        command = args.get("command")
        if not isinstance(command, (str, list)) or (isinstance(command, list) and not all(isinstance(x, str) for x in command)):
            raise ValueError("command must be a string or string array")
        cwd = str(Path(args.get("cwd") or os.getcwd()).expanduser().resolve())
        if not Path(cwd).is_dir():
            raise ValueError(f"cwd does not exist: {cwd}")
        env = os.environ.copy()
        if args.get("env") is not None:
            if not isinstance(args["env"], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in args["env"].items()):
                raise ValueError("env must be an object of string values")
            env.update(args["env"])
        job_id = "job-" + uuid.uuid4().hex[:12]
        stdout_path, stderr_path = str(LOGS / f"{job_id}.stdout.log"), str(LOGS / f"{job_id}.stderr.log")
        out, err = open(stdout_path, "ab", buffering=0), open(stderr_path, "ab", buffering=0)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                    shell=isinstance(command, str), creationflags=flags, start_new_session=(os.name != "nt"))
        except Exception:
            out.close(); err.close()
            raise
        job = {"job_id": job_id, "name": args.get("name"), "command": json.dumps(command, ensure_ascii=False), "cwd": cwd,
               "pid": proc.pid, "start_time": now(), "end_time": None, "status": "running", "exit_code": None,
               "stdout_path": stdout_path, "stderr_path": stderr_path}
        self.save(job)
        self.jobs[job_id] = {**job, "proc": proc, "kill_requested": False}
        threading.Thread(target=self.watch, args=(job_id, proc, out, err), daemon=True).start()
        return self.public(job)

    def watch(self, job_id: str, proc: subprocess.Popen[bytes], out: Any, err: Any) -> None:
        code = proc.wait()
        out.close(); err.close()
        job = self.jobs.get(job_id) or self.row(job_id)
        if not job:
            return
        job.update(end_time=now(), exit_code=code, status="killed" if job.get("kill_requested") else ("completed" if code == 0 else "failed"))
        self.save({k: v for k, v in job.items() if k not in ("proc", "kill_requested")})
        self.jobs.pop(job_id, None)
        with self.done:
            self.done.notify_all()

    def wait(self, ids: list[str] | None, mode: str, cancelled: threading.Event | None = None) -> list[dict[str, Any]]:
        if mode not in ("all", "any"):
            raise ValueError("mode must be all or any")
        if ids is None:
            ids = [j["job_id"] for j in self.jobs_for("running")]
        if not ids:
            return []
        jobs = []
        for job_id in ids:
            job = self.jobs.get(job_id) or self.row(job_id)
            if not job:
                raise ValueError(f"unknown job_id: {job_id}")
            jobs.append(job)
        with self.done:
            while True:
                if cancelled and cancelled.is_set():
                    raise RequestCancelled("wait request cancelled")
                current = [self.row(job["job_id"]) or job for job in jobs]
                finished = [job for job in current if job.get("status") != "running"]
                if (mode == "all" and len(finished) == len(current)) or (mode == "any" and finished):
                    result = finished if mode == "any" else current
                    return [self.public(job) for job in result]
                # Internal cancellation check only; the caller still receives
                # one response after the real process completion.
                self.done.wait(timeout=0.25 if cancelled else None)

    def output(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self.row(args["job_id"])
        if not job:
            raise ValueError(f"unknown job_id: {args['job_id']}")
        n, offsets = int(args.get("tail_lines", TAIL_LINES)), args.get("offset") or {}
        result: dict[str, Any] = {"job_id": job["job_id"], "status": job["status"], "stdout_path": job["stdout_path"], "stderr_path": job["stderr_path"]}
        for stream, key in (("stdout", "stdout_path"), ("stderr", "stderr_path")):
            path = Path(job[key])
            try:
                with path.open("rb") as f:
                    f.seek(int(offsets.get(stream, 0)))
                    data = f.read(MAX_OUTPUT_BYTES)
                    result[stream], result[f"{stream}_next_offset"] = data.decode("utf-8", "replace"), f.tell()
                    result[f"{stream}_truncated"] = f.read(1) != b""
            except OSError:
                result[stream], result[f"{stream}_next_offset"] = "", 0
            result[f"{stream}_tail"] = tail(str(path), n)
        return result

    def kill(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = args["job_id"]
        job = self.jobs.get(job_id) or self.row(job_id)
        if not job:
            raise ValueError(f"unknown job_id: {job_id}")
        if job.get("status") != "running":
            return self.public(job)
        timeout = max(0.0, float(args.get("timeout_sec", 5)))
        job["kill_requested"] = True
        proc = job.get("proc")
        if proc is not None:
            try:
                if os.name != "nt":
                    os.killpg(proc.pid, signal.SIGTERM)
                elif hasattr(signal, "CTRL_BREAK_EVENT"):
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.kill_tree(proc.pid, timeout_sec=max(1.0, timeout))
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        else:
            self.kill_tree(job["pid"], timeout_sec=max(1.0, timeout))
        return self.public(self.row(job_id) or job)

    @staticmethod
    def kill_tree(pid: int, timeout_sec: float = 5) -> None:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=max(1.0, timeout_sec),
                )
            except subprocess.TimeoutExpired:
                pass
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass

    @staticmethod
    def public(job: dict[str, Any]) -> dict[str, Any]:
        result = {k: job.get(k) for k in ("job_id", "name", "pid", "status", "exit_code", "stdout_path", "stderr_path")}
        result.update(command=json.loads(job["command"]) if isinstance(job.get("command"), str) else job.get("command"), cwd=job.get("cwd"), start_time=iso(job.get("start_time")), end_time=iso(job.get("end_time")))
        if job.get("end_time"):
            result["duration_sec"] = round(job["end_time"] - job["start_time"], 3)
        result["stdout_tail"], result["stderr_tail"] = tail(job.get("stdout_path", "")), tail(job.get("stderr_path", ""))
        return result


def call(store: JobStore, name: str, args: dict[str, Any], cancelled: threading.Event | None = None) -> Any:
    if name == "run": return store.run(args)
    if name == "wait": return store.wait(args.get("job_ids"), args.get("mode", "all"), cancelled)
    if name == "output": return store.output(args)
    if name == "kill": return store.kill(args)
    if name == "list": return [store.public(j) for j in store.jobs_for(args.get("status"), args.get("cwd"))]
    raise ValueError(f"unknown tool: {name}")


def main() -> None:
    store = JobStore()
    active: dict[Any, threading.Event] = {}
    active_lock = threading.Lock()
    write_lock = threading.Lock()

    def emit(response: dict[str, Any]) -> None:
        with write_lock:
            print(json.dumps(response, ensure_ascii=False), flush=True)

    def handle(request: dict[str, Any], cancelled: threading.Event) -> None:
        request_id = request["id"]
        try:
            method = request.get("method")
            if method == "initialize":
                result = {"protocolVersion": request.get("params", {}).get("protocolVersion", "2025-03-26"), "capabilities": {"tools": {}}, "serverInfo": {"name": "wait-mcp", "version": "0.2.0"}}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = request.get("params", {})
                result = {"content": [{"type": "text", "text": json.dumps(call(store, params["name"], params.get("arguments") or {}, cancelled), ensure_ascii=False)}]}
            else:
                raise ValueError(f"unsupported method: {method}")
            emit({"jsonrpc": "2.0", "id": request_id, "result": result})
        except RequestCancelled as exc:
            emit({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32800, "message": str(exc)}})
        except Exception as exc:
            emit({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(exc)}})
        finally:
            with active_lock:
                active.pop(request_id, None)

    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        method = request.get("method")
        if method in ("notifications/cancelled", "$/cancelRequest"):
            request_id = request.get("params", {}).get("requestId")
            with active_lock:
                cancelled = active.get(request_id)
            if cancelled:
                cancelled.set()
                with store.done:
                    store.done.notify_all()
            continue
        if "id" not in request:
            continue
        cancelled = threading.Event()
        with active_lock:
            active[request["id"]] = cancelled
        threading.Thread(target=handle, args=(request, cancelled), daemon=True).start()


if __name__ == "__main__":
    main()

