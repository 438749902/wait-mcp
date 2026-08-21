import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parent


def run_policy(command):
    result = subprocess.run(
        [sys.executable, str(ROOT / "wait_mcp_policy.py")],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def main():
    blocked = run_policy("nohup python train.py")
    assert blocked["hookSpecificOutput"]["permissionDecision"] == "deny", blocked
    assert run_policy("nohup python train.py > train.log 2>&1 & # wait-mcp: user-nohup") is None
    still_blocked = run_policy("Start-Process python train.py # wait-mcp: user-nohup")
    assert still_blocked["hookSpecificOutput"]["permissionDecision"] == "deny", still_blocked
    print("policy-test passed")


if __name__ == "__main__":
    main()
