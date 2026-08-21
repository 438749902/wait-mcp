#!/usr/bin/env python3
"""Small Codex PreToolUse guard for long-running experiment launches."""

from __future__ import annotations

import json
import re
import sys


DETACHED = re.compile(
    r"\b(?:nohup|start\s+/b|start-process|start-job)\b|(?<!&)\s&\s*(?:$|2?>)",
    re.IGNORECASE,
)
NOHUP = re.compile(r"\bnohup\b", re.IGNORECASE)
OTHER_DETACHED = re.compile(r"\b(?:start\s+/b|start-process|start-job)\b", re.IGNORECASE)
USER_NOHUP = re.compile(r"(?:#|\brem\b)\s*wait-mcp:\s*user-nohup\b", re.IGNORECASE)


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return
    if event.get("tool_name") != "Bash":
        return
    command = str((event.get("tool_input") or {}).get("command") or "")
    manual_nohup = NOHUP.search(command) and USER_NOHUP.search(command) and not OTHER_DETACHED.search(command)
    if not DETACHED.search(command) or manual_nohup:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Do not detach long-running experiments by default with nohup, Start-Process, "
                "Start-Job, start /b, or shell background '&'. Use wait-mcp.run_and_wait "
                "for one experiment, or wait-mcp.run followed immediately by wait for "
                "explicitly concurrent experiments. If the user explicitly requested "
                "manual nohup, append '# wait-mcp: user-nohup' to that command."
            ),
        }
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
