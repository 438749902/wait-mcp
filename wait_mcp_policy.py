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


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return
    if event.get("tool_name") != "Bash":
        return
    command = str((event.get("tool_input") or {}).get("command") or "")
    if not DETACHED.search(command):
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Do not detach long-running experiments with nohup, Start-Process, "
                "Start-Job, start /b, or shell background '&'. Use wait-mcp.run_and_wait "
                "for one experiment, or wait-mcp.run followed immediately by wait for "
                "explicitly concurrent experiments."
            ),
        }
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
