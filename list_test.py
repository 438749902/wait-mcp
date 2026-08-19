import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).parent
HOME = ROOT / ".list-test-home"


def send(proc, request):
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()


def read(proc):
    return json.loads(proc.stdout.readline())


def tool(proc, request_id, name, arguments):
    send(proc, {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    response = read(proc)
    return json.loads(response["result"]["content"][0]["text"])


def main():
    shutil.rmtree(HOME, ignore_errors=True)
    cwd_a, cwd_b = ROOT / ".list-cwd-a", ROOT / ".list-cwd-b"
    cwd_a.mkdir(exist_ok=True)
    cwd_b.mkdir(exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "wait_mcp.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "WAIT_MCP_HOME": str(HOME)},
    )
    responses = queue.Queue()

    def reader():
        for line in proc.stdout:
            responses.put(json.loads(line))

    try:
        tool(proc, 1, "list", {})
        job = tool(proc, 2, "run", {"command": [sys.executable, str(ROOT / "dummy_experiment.py"), "30"], "cwd": str(cwd_a)})
        other = tool(proc, 6, "run", {"command": [sys.executable, str(ROOT / "dummy_experiment.py"), "30"], "cwd": str(cwd_b)})
        threading.Thread(target=reader, daemon=True).start()

        send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "wait", "arguments": {"job_ids": [job["job_id"]]}}})
        list_sent = time.monotonic()
        send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "list", "arguments": {"status": "running", "cwd": str(cwd_a)}}})
        list_response = responses.get(timeout=2)
        list_elapsed = time.monotonic() - list_sent
        assert list_response["id"] == 4, list_response
        listed = json.loads(list_response["result"]["content"][0]["text"])
        assert listed and all(item["job_id"] == job["job_id"] and item["status"] == "running" for item in listed), listed

        send(proc, {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}})
        cancelled = responses.get(timeout=2)
        assert cancelled["id"] == 3 and cancelled["error"]["code"] == -32800, cancelled
        kill_started = time.monotonic()
        send(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "kill", "arguments": {"job_id": job["job_id"], "timeout_sec": 1}}})
        kill_response = responses.get(timeout=3)
        kill_elapsed = time.monotonic() - kill_started
        kill = json.loads(kill_response["result"]["content"][0]["text"])
        assert kill["status"] in ("killed", "running"), kill
        send(proc, {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "kill", "arguments": {"job_id": other["job_id"], "timeout_sec": 1}}})
        other_response = responses.get(timeout=3)
        assert other_response["id"] == 7, other_response
        print(json.dumps({"list_elapsed_sec": round(list_elapsed, 3), "kill_elapsed_sec": round(kill_elapsed, 3), "listed_status": "running", "test": "list while wait is blocked"}))
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(cwd_a, ignore_errors=True)
        shutil.rmtree(cwd_b, ignore_errors=True)


if __name__ == "__main__":
    main()

