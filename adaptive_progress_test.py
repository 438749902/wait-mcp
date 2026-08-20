import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parent
HOME = ROOT / ".adaptive-progress-test-home"


def call(proc, request_id, name, arguments):
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}) + "\n")
    proc.stdin.flush()
    response = json.loads(proc.stdout.readline())
    if "error" in response:
        raise RuntimeError(response["error"])
    return json.loads(response["result"]["content"][0]["text"])


def main():
    shutil.rmtree(HOME, ignore_errors=True)
    env = {**os.environ, "WAIT_MCP_HOME": str(HOME)}
    def start():
        return subprocess.Popen([sys.executable, str(ROOT / "wait_mcp.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)

    proc = start()
    try:
        completed = call(proc, 1, "run_and_wait", {
            "command": [sys.executable, str(ROOT / "dummy_experiment.py"), "6"],
            "progress": {"sample_steps": 2, "sample_timeout_sec": 2, "min_hard_timeout_sec": 4},
        })
        assert completed["status"] == "completed", completed
        assert completed["progress"]["sampled"], completed
        assert completed["progress"]["current_step"] == 6, completed
        assert 4 < completed["progress"]["estimated_duration_sec"] < 8, completed

        recovered_job = call(proc, 2, "run", {
            "command": [sys.executable, str(ROOT / "dummy_experiment.py"), "6"],
            "progress": {"total_steps": 6, "sample_steps": 2, "sample_timeout_sec": 2, "min_hard_timeout_sec": 4},
        })
        proc.terminate()
        proc.wait(timeout=5)
        proc = start()
        recovered = call(proc, 3, "wait", {"job_ids": [recovered_job["job_id"]]})[0]
        assert recovered["status"] == "completed", recovered
        assert recovered["progress"]["current_step"] == 6, recovered

        timed_out = call(proc, 2, "run_and_wait", {
            "command": [sys.executable, str(ROOT / "stalled_experiment.py")],
            "progress": {"total_steps": 4, "sample_steps": 1, "sample_timeout_sec": 1, "min_hard_timeout_sec": 2},
        })
        assert timed_out["status"] == "timed_out", timed_out
        assert timed_out["progress"]["hard_deadline"], timed_out
        print(json.dumps({"completed": completed["progress"], "recovered": recovered["status"], "timed_out": timed_out["status"]}))
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
