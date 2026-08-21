import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).parent
HOME = ROOT / ".test-home"


def send(proc, request):
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def call(proc, request_id, name, arguments):
    response = send(proc, {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    return json.loads(response["result"]["content"][0]["text"])


def main():
    shutil.rmtree(HOME, ignore_errors=True)
    proc = subprocess.Popen([sys.executable, str(ROOT / "wait_mcp.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env={**os.environ, "WAIT_MCP_HOME": str(HOME)})
    initialized = send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert "run_and_wait" in initialized["result"]["instructions"]
    tools = send(proc, {"jsonrpc": "2.0", "id": 1.5, "method": "tools/list"})
    assert "run_and_wait" in {tool["name"] for tool in tools["result"]["tools"]}
    blocked = send(proc, {"jsonrpc": "2.0", "id": 1.75, "method": "tools/call", "params": {"name": "run", "arguments": {"command": "nohup python train.py"}}})
    assert "detached experiment launch" in blocked["error"]["message"]
    manual = call(proc, 1.8, "run", {"command": f'nohup "{sys.executable}" -c "print(1)"', "allow_manual_nohup": True})
    assert manual["status"] in ("running", "completed", "failed"), manual
    if manual["status"] == "running":
        assert call(proc, 1.9, "wait", {"job_ids": [manual["job_id"]]})[0]["status"] in ("completed", "failed")
    one = call(proc, 2, "run", {"command": [sys.executable, str(ROOT / "dummy_experiment.py"), "2"], "name": "two-second"})
    assert one["status"] == "running"
    current = call(proc, 3, "output", {"job_id": one["job_id"], "tail_lines": 1})
    assert "stdout" in current
    started = time.monotonic()
    done = call(proc, 4, "wait", {"job_ids": [one["job_id"]]})
    assert done[0]["status"] == "completed" and done[0]["exit_code"] == 0
    assert time.monotonic() - started >= 1.0
    blocking = call(proc, 5, "run_and_wait", {"command": [sys.executable, str(ROOT / "dummy_experiment.py"), "1"]})
    assert blocking["status"] == "completed" and blocking["exit_code"] == 0
    two = call(proc, 6, "run", {"command": [sys.executable, str(ROOT / "dummy_experiment.py"), "10"]})
    killed = call(proc, 7, "kill", {"job_id": two["job_id"]})
    assert killed["status"] in ("killed", "running")
    listed = call(proc, 8, "list", {})
    assert {j["job_id"] for j in listed} >= {one["job_id"], two["job_id"], blocking["job_id"]}
    proc.terminate(); proc.wait(timeout=5)
    print("self-test passed")


if __name__ == "__main__":
    main()
