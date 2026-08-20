import json
import os
import subprocess
import sys
import shutil
from pathlib import Path


ROOT = Path(__file__).parent
HOME = ROOT / ".restart-test-home"
shutil.rmtree(HOME, ignore_errors=True)
env = {**os.environ, "WAIT_MCP_HOME": str(HOME)}
proc = subprocess.Popen([sys.executable, str(ROOT / "wait_mcp.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)


def request(request_id, method, params):
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


request(1, "initialize", {})
response = request(2, "tools/call", {"name": "list", "arguments": {}})
jobs = json.loads(response["result"]["content"][0]["text"])
assert all(job["status"] != "running" for job in jobs), jobs
proc.terminate(); proc.wait(timeout=5)
shutil.rmtree(HOME, ignore_errors=True)
print(json.dumps({"durable_jobs_seen": len(jobs), "running_after_restart": sum(job["status"] == "running" for job in jobs)}))
