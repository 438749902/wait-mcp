#!/usr/bin/env python3
"""Small stdio MCP server for durable, blocking shell jobs."""

from __future__ import annotations

import ctypes
import json
import math
import os
import re
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
DEFAULT_PROGRESS_PATTERN = r"(?i)\b(?:epoch|step|round|iteration|iter)\s*[=:]?\s*(\d+)\s*(?:/|of)\s*(\d+)"
DEFAULT_SAMPLE_TIMEOUT_SEC = 300.0
DEFAULT_REVIEW_INTERVAL_SEC = 300.0
ERROR_PATTERNS = {
    "traceback": re.compile(r"Traceback \(most recent call last\):", re.IGNORECASE),
    "pickling_error": re.compile(r"PicklingError|pickle\.(Pickling|Unpickling)Error", re.IGNORECASE),
    "cuda_error": re.compile(r"CUDA out of memory|CUDA error|CUBLAS_STATUS|device-side assert", re.IGNORECASE),
    "runtime_error": re.compile(r"(?:^|\b)(?:RuntimeError|Fatal|OOMKilled|OutOfMemoryError|No space left)(?:\b|:)", re.IGNORECASE),
}
SERVER_INSTRUCTIONS = (
    "For any long-running training or experiment, call run_and_wait instead of shell. "
    "It starts the job and blocks until completion in this MCP call. Use run only for "
    "explicit concurrency, then call wait immediately before doing other work. Never "
    "launch experiments with nohup, shell background &, Start-Process, or detached "
    "redirection. For step-based experiments, pass progress.total_steps when known "
    "or emit step/epoch/round/iteration N/M; the server will estimate timing and "
    "use the estimate as a review checkpoint, never kill solely because the estimate "
    "was exceeded. On review_required, inspect diagnostics/output, then call wait "
    "to resume or kill only after confirming a failure."
)
DETACHED_COMMAND = re.compile(
    r"\b(?:nohup|start\s+/b|start-process|start-job)\b|(?<!&)\s&\s*(?:$|2?>)",
    re.IGNORECASE,
)


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


COMMAND_SCHEMA = {"description": "Shell command string or argv array.", "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}
PROGRESS_SCHEMA = {"type": "object", "properties": {
    "total_steps": {"type": "integer", "minimum": 1},
    "sample_steps": {"type": "integer", "minimum": 1},
    "pattern": {"type": "string", "description": "Regex with current step in group 1 and optional total in group 2."},
    "sample_timeout_sec": {"type": "number", "minimum": 1, "default": DEFAULT_SAMPLE_TIMEOUT_SEC},
    "review_interval_sec": {"type": "number", "minimum": 1, "default": DEFAULT_REVIEW_INTERVAL_SEC},
    "min_hard_timeout_sec": {"type": "number", "minimum": 1, "description": "Deprecated compatibility field; never causes automatic termination."},
}}
COMMON_RUN_PROPERTIES = {"command": COMMAND_SCHEMA, "cwd": {"type": "string"}, "env": {"type": "object", "additionalProperties": {"type": "string"}}, "name": {"type": "string"}, "progress": PROGRESS_SCHEMA}


TOOLS = [
    {"name": "run_and_wait", "description": "Preferred path for one long-running experiment: start the job and block in this MCP call until it completes. Use instead of run unless explicit concurrency is requested.", "inputSchema": {"type": "object", "required": ["command"], "properties": COMMON_RUN_PROPERTIES}},
    {"name": "run", "description": "Start a shell job and return immediately with a durable job_id. Use only when you need concurrent jobs; call wait immediately after run before doing other work.", "inputSchema": {"type": "object", "required": ["command"], "properties": COMMON_RUN_PROPERTIES}},
    {"name": "wait", "description": "Block inside the MCP call until one or all target jobs finish. Never polls the caller. After run, this should be the next tool call. Supports MCP request cancellation.", "inputSchema": {"type": "object", "properties": {"job_ids": {"type": "array", "items": {"type": "string"}}, "mode": {"type": "string", "enum": ["all", "any"], "default": "all"}}}},
    {"name": "output", "description": "Read current output without waiting for completion. Do not use it as a polling substitute for wait.", "inputSchema": {"type": "object", "required": ["job_id"], "properties": {"job_id": {"type": "string"}, "tail_lines": {"type": "integer", "minimum": 1}, "offset": {"type": "object", "properties": {"stdout": {"type": "integer", "minimum": 0}, "stderr": {"type": "integer", "minimum": 0}}}}}},
    {"name": "kill", "description": "Gracefully terminate a job, then kill its process tree after a short timeout.", "inputSchema": {"type": "object", "required": ["job_id"], "properties": {"job_id": {"type": "string"}, "timeout_sec": {"type": "number", "minimum": 0, "default": 5}}}},
    {"name": "list", "description": "List durable jobs by status, optionally filtered by working directory.", "inputSchema": {"type": "object", "properties": {"status": {"type": "string", "enum": ["running", "completed", "failed", "killed", "timed_out"]}, "cwd": {"type": "string", "description": "Only return jobs whose normalized working directory matches this path."}}}},
]


def make_monitor(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("progress must be an object")
    pattern = value.get("pattern", DEFAULT_PROGRESS_PATTERN)
    if not isinstance(pattern, str):
        raise ValueError("progress.pattern must be a string")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid progress.pattern: {exc}") from exc
    if compiled.groups < 1:
        raise ValueError("progress.pattern must capture the current step in group 1")
    total = value.get("total_steps")
    if total is not None and (not isinstance(total, int) or total < 1):
        raise ValueError("progress.total_steps must be a positive integer")
    sample_steps = value.get("sample_steps")
    if sample_steps is not None and (not isinstance(sample_steps, int) or sample_steps < 1):
        raise ValueError("progress.sample_steps must be a positive integer")
    if sample_steps is None and total is not None:
        sample_steps = min(20, max(3, math.ceil(total * 0.02)))
    sample_timeout = max(1.0, float(value.get("sample_timeout_sec", DEFAULT_SAMPLE_TIMEOUT_SEC)))
    review_interval = max(1.0, float(value.get("review_interval_sec", DEFAULT_REVIEW_INTERVAL_SEC)))
    return {
        "pattern": pattern,
        "total_steps": total,
        "sample_steps": min(sample_steps, total) if sample_steps and total else sample_steps,
        "sample_timeout_sec": sample_timeout,
        "review_interval_sec": review_interval,
        "current_step": 0,
        "observed_total_steps": total,
        "sampled": False,
        "checkpointed": False,
        "estimated_duration_sec": None,
        "avg_step_sec": None,
        "checkpoint_at": None,
        "hard_deadline": None,
        "last_progress_at": None,
        "sample_step": None,
        "sample_time": None,
        "review_required": False,
        "review_id": 0,
        "diagnostics": None,
    }


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
                status TEXT NOT NULL, exit_code INTEGER, stdout_path TEXT NOT NULL, stderr_path TEXT NOT NULL,
                monitor_json TEXT NOT NULL DEFAULT '{}'
            )""")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
            if "monitor_json" not in columns:
                self.db.execute("ALTER TABLE jobs ADD COLUMN monitor_json TEXT NOT NULL DEFAULT '{}'")
            self.db.commit()
        self.recover()

    def row(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return self.hydrate(dict(row)) if row else None

    @staticmethod
    def hydrate(job: dict[str, Any]) -> dict[str, Any]:
        raw = job.pop("monitor_json", "{}")
        try:
            job["monitor"] = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            job["monitor"] = {}
        if job["monitor"]:
            job["monitor"].setdefault("review_interval_sec", DEFAULT_REVIEW_INTERVAL_SEC)
            job["monitor"].setdefault("review_required", False)
            job["monitor"].setdefault("review_id", 0)
            job["monitor"].setdefault("diagnostics", None)
        return job

    def save(self, job: dict[str, Any]) -> None:
        payload = {key: job.get(key) for key in ("job_id", "name", "command", "cwd", "pid", "start_time", "end_time", "status", "exit_code", "stdout_path", "stderr_path")}
        payload["monitor_json"] = json.dumps(job.get("monitor") or {}, ensure_ascii=False)
        with self.lock:
            self.db.execute("""INSERT OR REPLACE INTO jobs
                (job_id,name,command,cwd,pid,start_time,end_time,status,exit_code,stdout_path,stderr_path,monitor_json)
                VALUES (:job_id,:name,:command,:cwd,:pid,:start_time,:end_time,:status,:exit_code,:stdout_path,:stderr_path,:monitor_json)""", payload)
            self.db.commit()

    def jobs_for(self, status: str | None = None, cwd: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            query = "SELECT * FROM jobs" + (" WHERE status=?" if status else "") + " ORDER BY start_time"
            rows = self.db.execute(query, ((status,) if status else ())).fetchall()
        jobs = [self.hydrate(dict(r)) for r in rows]
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
                watcher = self.monitor_recovered if job.get("monitor") else self.recover_watch
                threading.Thread(target=watcher, args=(job,), daemon=True).start()
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

    def monitor_recovered(self, job: dict[str, Any]) -> None:
        offset = 0
        monitor = job["monitor"]
        while pid_alive(job["pid"]):
            offset, changed = self.ingest_progress(job, offset)
            if changed:
                self.save(job)
            current_time = now()
            if not monitor.get("sampled"):
                sample_deadline = job["start_time"] + monitor["sample_timeout_sec"]
                if current_time >= sample_deadline:
                    monitor.update(sampled=True, estimated_duration_sec=monitor["sample_timeout_sec"], checkpoint_at=sample_deadline)
                    self.save(job)
                else:
                    time.sleep(min(1.0, sample_deadline - current_time))
                    continue
            target = monitor["checkpoint_at"]
            if target - now() <= 0:
                self.review_monitor(job)
                self.save(job)
                while monitor.get("review_required") and pid_alive(job["pid"]):
                    time.sleep(1)
                continue
            time.sleep(min(1.0, target - now()))
        code = recovered_exit_code(job["pid"])
        status = "timed_out" if job.get("timeout_requested") else ("completed" if code == 0 else "failed")
        job.update(status=status, end_time=now(), exit_code=code)
        self.save(job)
        self.jobs.pop(job["job_id"], None)
        with self.done:
            self.done.notify_all()

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        command = args.get("command")
        if not isinstance(command, (str, list)) or (isinstance(command, list) and not all(isinstance(x, str) for x in command)):
            raise ValueError("command must be a string or string array")
        command_text = command if isinstance(command, str) else " ".join(command)
        if DETACHED_COMMAND.search(command_text):
            raise ValueError("detached experiment launch is not supported; use run_and_wait or run followed by wait")
        cwd = str(Path(args.get("cwd") or os.getcwd()).expanduser().resolve())
        if not Path(cwd).is_dir():
            raise ValueError(f"cwd does not exist: {cwd}")
        env = os.environ.copy()
        if args.get("env") is not None:
            if not isinstance(args["env"], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in args["env"].items()):
                raise ValueError("env must be an object of string values")
            env.update(args["env"])
        monitor = make_monitor(args.get("progress"))
        job_id = "job-" + uuid.uuid4().hex[:12]
        stdout_path, stderr_path = str(LOGS / f"{job_id}.stdout.log"), str(LOGS / f"{job_id}.stderr.log")
        out, err = open(stdout_path, "ab", buffering=0), open(stderr_path, "ab", buffering=0)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        start_time = now()
        try:
            proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                    shell=isinstance(command, str), creationflags=flags, start_new_session=(os.name != "nt"))
        except Exception:
            out.close(); err.close()
            raise
        job = {"job_id": job_id, "name": args.get("name"), "command": json.dumps(command, ensure_ascii=False), "cwd": cwd,
               "pid": proc.pid, "start_time": start_time, "end_time": None, "status": "running", "exit_code": None,
               "stdout_path": stdout_path, "stderr_path": stderr_path, "monitor": monitor or {}}
        self.save(job)
        self.jobs[job_id] = {**job, "proc": proc, "kill_requested": False}
        threading.Thread(target=self.watch, args=(job_id, proc, out, err), daemon=True).start()
        if monitor:
            threading.Thread(target=self.monitor_job, args=(job_id, proc), daemon=True).start()
        return self.public(job)

    def watch(self, job_id: str, proc: subprocess.Popen[bytes], out: Any, err: Any) -> None:
        code = proc.wait()
        out.close(); err.close()
        job = self.jobs.get(job_id) or self.row(job_id)
        if not job:
            return
        status = "timed_out" if job.get("timeout_requested") else ("killed" if job.get("kill_requested") else ("completed" if code == 0 else "failed"))
        job.update(end_time=now(), exit_code=code, status=status)
        self.save({k: v for k, v in job.items() if k not in ("proc", "kill_requested")})
        self.jobs.pop(job_id, None)
        with self.done:
            self.done.notify_all()

    @staticmethod
    def read_progress(path: str, offset: int) -> tuple[int, list[str]]:
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read()
                return f.tell(), data.decode("utf-8", "replace").splitlines()
        except OSError:
            return offset, []

    def ingest_progress(self, job: dict[str, Any], offset: int) -> tuple[int, bool]:
        monitor = job.get("monitor") or {}
        if not monitor:
            return offset, False
        offset, lines = self.read_progress(job["stdout_path"], offset)
        if not lines:
            return offset, False
        regex = re.compile(monitor["pattern"])
        changed = False
        for line in lines:
            match = regex.search(line)
            if not match:
                continue
            try:
                current = int(match.group(1))
                observed_total = int(match.group(2)) if match.lastindex and match.lastindex >= 2 else None
            except (TypeError, ValueError):
                continue
            total = observed_total or monitor.get("total_steps")
            if total:
                monitor["observed_total_steps"] = total
                if not monitor.get("sample_steps"):
                    monitor["sample_steps"] = min(20, max(3, math.ceil(total * 0.02)))
            if current <= monitor.get("current_step", 0):
                continue
            timestamp = now()
            monitor.update(current_step=current, last_progress_at=timestamp)
            changed = True
            sample_steps = monitor.get("sample_steps")
            if not monitor.get("sampled") and total and sample_steps and current >= sample_steps:
                elapsed = max(timestamp - job["start_time"], 0.001)
                average = elapsed / current
                estimate = elapsed + max(total - current, 0) * average
                monitor.update(
                    sampled=True,
                    sample_step=current,
                    sample_time=timestamp,
                    avg_step_sec=average,
                    estimated_duration_sec=estimate,
                    checkpoint_at=job["start_time"] + estimate,
                )
        return offset, changed

    def diagnose(self, job: dict[str, Any]) -> dict[str, Any]:
        stdout_tail, stderr_tail = tail(job["stdout_path"], 50), tail(job["stderr_path"], 50)
        signals = []
        for name, pattern in ERROR_PATTERNS.items():
            if pattern.search("\n".join(stdout_tail + stderr_tail)):
                signals.append(name)
        monitor = job["monitor"]
        return {
            "at": iso(now()),
            "classification": "possible_failure" if signals else "still_running",
            "fatal_signals": signals,
            "current_step": monitor.get("current_step", 0),
            "total_steps": monitor.get("observed_total_steps"),
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        }

    def review_monitor(self, job: dict[str, Any]) -> None:
        monitor = job["monitor"]
        self.checkpoint_monitor(job)
        current, total, average = monitor.get("current_step", 0), monitor.get("observed_total_steps"), monitor.get("avg_step_sec")
        remaining = (total - current) * average if total and average and current < total else 0
        monitor.update(
            review_required=True,
            review_id=monitor.get("review_id", 0) + 1,
            diagnostics=self.diagnose(job),
            checkpoint_at=now() + max(remaining, monitor["review_interval_sec"]),
        )

    @staticmethod
    def acknowledge_review(job: dict[str, Any]) -> bool:
        monitor = job.get("monitor") or {}
        if not monitor.get("review_required"):
            return False
        monitor["review_required"] = False
        monitor["checkpointed"] = False
        return True

    @staticmethod
    def checkpoint_monitor(job: dict[str, Any]) -> None:
        monitor = job.get("monitor") or {}
        if not monitor or monitor.get("checkpointed"):
            return
        monitor["checkpointed"] = True
        sample_step, sample_time = monitor.get("sample_step"), monitor.get("sample_time")
        current, total = monitor.get("current_step", 0), monitor.get("observed_total_steps")
        if sample_step and sample_time and total and current > sample_step:
            average = max((now() - sample_time) / (current - sample_step), 0.001)
            estimate = now() - job["start_time"] + max(total - current, 0) * average
            monitor.update(avg_step_sec=average, estimated_duration_sec=estimate)

    def monitor_job(self, job_id: str, proc: subprocess.Popen[bytes]) -> None:
        job = self.jobs.get(job_id) or self.row(job_id)
        if not job or not job.get("monitor"):
            return
        offset = 0
        monitor = job["monitor"]
        while True:
            offset, changed = self.ingest_progress(job, offset)
            if changed:
                self.save(job)
            if proc.poll() is not None:
                return
            current_time = now()
            if not monitor.get("sampled"):
                sample_deadline = job["start_time"] + monitor["sample_timeout_sec"]
                if current_time >= sample_deadline:
                    monitor.update(sampled=True, estimated_duration_sec=monitor["sample_timeout_sec"], checkpoint_at=sample_deadline)
                    self.save(job)
                    continue
                try:
                    proc.wait(timeout=min(1.0, sample_deadline - current_time))
                except subprocess.TimeoutExpired:
                    pass
                continue
            target = monitor["checkpoint_at"]
            remaining = target - now()
            if remaining <= 0:
                self.review_monitor(job)
                self.save(job)
                with self.done:
                    self.done.notify_all()
                    while monitor.get("review_required") and proc.poll() is None:
                        self.done.wait()
                continue
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass

    def wait(self, ids: list[str] | None, mode: str, cancelled: threading.Event | None = None, resume: bool = True) -> list[dict[str, Any]]:
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
        if resume:
            changed = any(self.acknowledge_review(job) for job in jobs)
            if changed:
                for job in jobs:
                    self.save(job)
                with self.done:
                    self.done.notify_all()
        with self.done:
            while True:
                if cancelled and cancelled.is_set():
                    raise RequestCancelled("wait request cancelled")
                current = [self.row(job["job_id"]) or job for job in jobs]
                reviews = [job for job in current if (job.get("monitor") or {}).get("review_required")]
                if reviews:
                    return [self.public(job) for job in reviews]
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
        monitor = job.get("monitor") or {}
        if monitor:
            if monitor.get("review_required"):
                result["status"] = "review_required"
                result["review"] = monitor.get("diagnostics")
            result["progress"] = {
                "current_step": monitor.get("current_step", 0),
                "total_steps": monitor.get("observed_total_steps"),
                "sample_steps": monitor.get("sample_steps"),
                "sampled": bool(monitor.get("sampled")),
                "checkpointed": bool(monitor.get("checkpointed")),
                "estimated_duration_sec": monitor.get("estimated_duration_sec"),
                "avg_step_sec": monitor.get("avg_step_sec"),
                "checkpoint_at": iso(monitor.get("checkpoint_at")),
                "review_required": bool(monitor.get("review_required")),
                "review_id": monitor.get("review_id", 0),
                "last_progress_at": iso(monitor.get("last_progress_at")),
            }
        result["stdout_tail"], result["stderr_tail"] = tail(job.get("stdout_path", "")), tail(job.get("stderr_path", ""))
        return result


def call(store: JobStore, name: str, args: dict[str, Any], cancelled: threading.Event | None = None) -> Any:
    if name == "run_and_wait":
        started = store.run(args)
        return store.wait([started["job_id"]], "all", cancelled, resume=False)[0]
    if name == "run": return store.run(args)
    if name == "wait": return store.wait(args.get("job_ids"), args.get("mode", "all"), cancelled, resume=True)
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
            print(json.dumps(response, ensure_ascii=True), flush=True)

    def handle(request: dict[str, Any], cancelled: threading.Event) -> None:
        request_id = request["id"]
        try:
            method = request.get("method")
            if method == "initialize":
                result = {"protocolVersion": request.get("params", {}).get("protocolVersion", "2025-03-26"), "capabilities": {"tools": {}}, "serverInfo": {"name": "wait-mcp", "version": "0.3.0"}, "instructions": SERVER_INSTRUCTIONS}
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

