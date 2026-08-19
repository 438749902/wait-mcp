import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).parent
OUT = ROOT.parent.parent / "outputs" / "acceptance-multi.json"
HOME = ROOT / ".acceptance-multi-home"


def send(proc, request):
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def call(proc, request_id, name, arguments):
    response = send(proc, {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    if "error" in response:
        raise RuntimeError(response["error"])
    return json.loads(response["result"]["content"][0]["text"])


def main():
    shutil.rmtree(HOME, ignore_errors=True)
    env = {**os.environ, "WAIT_MCP_HOME": str(HOME)}
    proc = subprocess.Popen([sys.executable, str(ROOT / "wait_mcp.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    long_job = call(proc, 2, "run", {"command": [sys.executable, str(ROOT / "dummy_experiment.py"), "300"], "name": "five-minute-kill-test"})
    time.sleep(1)
    output = call(proc, 3, "output", {"job_id": long_job["job_id"], "tail_lines": 2})
    killed_at = time.monotonic()
    kill_result = call(proc, 4, "kill", {"job_id": long_job["job_id"], "timeout_sec": 2})
    killed = call(proc, 5, "wait", {"job_ids": [long_job["job_id"]]})[0]
    listed = call(proc, 6, "list", {})
    a = call(proc, 7, "run", {"command": [sys.executable, str(ROOT / "dummy_experiment.py"), "2"], "name": "any-a"})
    b = call(proc, 8, "run", {"command": [sys.executable, str(ROOT / "dummy_experiment.py"), "4"], "name": "any-b"})
    any_started = time.monotonic()
    any_result = call(proc, 9, "wait", {"job_ids": [a["job_id"], b["job_id"]], "mode": "any"})
    any_sec = time.monotonic() - any_started
    all_started = time.monotonic()
    all_result = call(proc, 10, "wait", {"job_ids": [a["job_id"], b["job_id"]], "mode": "all"})
    all_sec = time.monotonic() - all_started
    proc.terminate(); proc.wait(timeout=5)
    result = {"five_minute_job": {"run": long_job, "output_while_running": output, "kill": kill_result, "final": killed, "kill_sec": round(time.monotonic() - killed_at, 3)}, "list_count": len(listed), "any": {"duration_sec": round(any_sec, 3), "result": any_result}, "all": {"duration_sec": round(all_sec, 3), "result": all_result}}
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"killed_status": killed["status"], "any_sec": round(any_sec, 3), "all_sec": round(all_sec, 3), "trace": str(OUT)}))


if __name__ == "__main__":
    main()
