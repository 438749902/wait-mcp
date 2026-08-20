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


def finish(proc, request_id, job_id):
    for _ in range(3):
        result = call(proc, request_id, "wait", {"job_ids": [job_id]})[0]
        if result["status"] != "review_required":
            return result
        request_id += 1
    raise AssertionError(result)


def main():
    shutil.rmtree(HOME, ignore_errors=True)
    env = {**os.environ, "WAIT_MCP_HOME": str(HOME)}
    def start():
        return subprocess.Popen([sys.executable, str(ROOT / "wait_mcp.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)

    proc = start()
    try:
        completed = call(proc, 1, "run_and_wait", {
            "command": [sys.executable, str(ROOT / "dummy_experiment.py"), "6"],
            "progress": {"sample_steps": 2, "sample_timeout_sec": 2, "review_interval_sec": 1},
        })
        if completed["status"] == "review_required":
            completed = finish(proc, 2, completed["job_id"])
        assert completed["status"] == "completed", completed
        assert completed["progress"]["sampled"], completed
        assert completed["progress"]["current_step"] == 6, completed
        assert 4 < completed["progress"]["estimated_duration_sec"] < 8, completed

        recovered_job = call(proc, 2, "run", {
            "command": [sys.executable, str(ROOT / "dummy_experiment.py"), "6"],
            "progress": {"total_steps": 6, "sample_steps": 2, "sample_timeout_sec": 2, "review_interval_sec": 1},
        })
        proc.terminate()
        proc.wait(timeout=5)
        proc = start()
        recovered = finish(proc, 3, recovered_job["job_id"])
        assert recovered["status"] == "completed", recovered
        assert recovered["progress"]["current_step"] == 6, recovered

        detached = call(proc, 4, "run_and_wait", {
            "command": [sys.executable, str(ROOT / "dummy_experiment.py"), "6"],
            "nohup_hours": 0.0001,
            "progress": {"sample_steps": 2, "sample_timeout_sec": 2, "review_interval_sec": 1},
        })
        assert detached["status"] == "nohup", detached
        assert detached["nohup"]["estimated_completion_at"], detached
        assert detached["nohup"]["next_query_at"], detached
        detached_done = finish(proc, 5, detached["job_id"])
        assert detached_done["status"] == "completed", detached_done

        reviewed = call(proc, 6, "run_and_wait", {
            "command": [sys.executable, str(ROOT / "stalled_experiment.py"), "error"],
            "progress": {"total_steps": 4, "sample_steps": 1, "sample_timeout_sec": 1, "review_interval_sec": 1},
        })
        assert reviewed["status"] == "review_required", reviewed
        assert reviewed["review"]["classification"] == "possible_failure", reviewed
        assert "pickling_error" in reviewed["review"]["fatal_signals"], reviewed
        call(proc, 7, "kill", {"job_id": reviewed["job_id"]})
        killed = call(proc, 8, "wait", {"job_ids": [reviewed["job_id"]]})[0]
        assert killed["status"] == "killed", killed
        print(json.dumps({"completed": completed["progress"], "recovered": recovered["status"], "nohup": detached["status"], "review": reviewed["status"], "killed": killed["status"]}))
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
