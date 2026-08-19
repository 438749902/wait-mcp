import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).parent
HOME = ROOT / ".control-test-home"


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
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "wait_mcp.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "WAIT_MCP_HOME": str(HOME)},
    )
    try:
        tool(proc, 1, "list", {})
        job = tool(proc, 2, "run", {"command": [sys.executable, str(ROOT / "dummy_experiment.py"), "30"]})
        send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "wait", "arguments": {"job_ids": [job["job_id"]]}}})
        send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kill", "arguments": {"job_id": job["job_id"], "timeout_sec": 1}}})
        responses = {response["id"]: response for response in (read(proc), read(proc))}
        kill_result = json.loads(responses[4]["result"]["content"][0]["text"])
        wait_result = json.loads(responses[3]["result"]["content"][0]["text"])[0]
        assert wait_result["status"] == "killed", wait_result
        assert kill_result["status"] in ("killed", "running"), kill_result

        second = tool(proc, 5, "run", {"command": [sys.executable, str(ROOT / "dummy_experiment.py"), "30"]})
        send(proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "wait", "arguments": {"job_ids": [second["job_id"]]}}})
        time.sleep(0.1)
        send(proc, {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 6}})
        cancelled = read(proc)
        assert cancelled["id"] == 6 and cancelled["error"]["code"] == -32800, cancelled
        tool(proc, 7, "kill", {"job_id": second["job_id"], "timeout_sec": 1})
        print("control-test passed: concurrent kill and MCP cancellation")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    main()
