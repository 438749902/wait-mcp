import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).parent
OUT = ROOT.parent.parent / "outputs" / "acceptance-120.json"
HOME = ROOT / ".acceptance-home"


def send(proc, request):
    sent = time.time()
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    response = json.loads(proc.stdout.readline())
    return response, sent, time.time()


def main():
    shutil.rmtree(HOME, ignore_errors=True)
    env = {**os.environ, "WAIT_MCP_HOME": str(HOME)}
    proc = subprocess.Popen([sys.executable, str(ROOT / "wait_mcp.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    events = []
    response, started, ended = send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    events.append({"kind": "initialize", "sent": started, "received": ended, "response": response})
    response, started, ended = send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert names == ["run", "wait", "output", "kill", "list"], names
    events.append({"kind": "tools/list", "sent": started, "received": ended, "tool_names": names})
    response, started, ended = send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "run", "arguments": {"command": [sys.executable, str(ROOT / "dummy_experiment.py"), "120"], "name": "dummy-120"}}})
    job = json.loads(response["result"]["content"][0]["text"])
    events.append({"kind": "run", "sent": started, "received": ended, "job": job})
    wait_sent = time.time()
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "wait", "arguments": {"job_ids": [job["job_id"]]}}}) + "\n")
    proc.stdin.flush()
    response = json.loads(proc.stdout.readline())
    wait_received = time.time()
    result = json.loads(response["result"]["content"][0]["text"])[0]
    events.append({"kind": "wait", "sent": wait_sent, "received": wait_received, "blocked_sec": round(wait_received - wait_sent, 3), "result": result})
    proc.terminate(); proc.wait(timeout=5)
    OUT.write_text(json.dumps({"test": "120-second blocking wait", "events": events, "intermediate_model_sampling": 0, "repeated_wait_calls": 0, "polling": 0}, indent=2), encoding="utf-8")
    print(json.dumps({"job_id": job["job_id"], "blocked_sec": round(wait_received - wait_sent, 3), "status": result["status"], "exit_code": result["exit_code"], "trace": str(OUT)}))


if __name__ == "__main__":
    main()
