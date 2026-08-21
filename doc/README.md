# wait-mcp

[中文 README](../README.md)

A Windows-first, stdlib-only MCP server for long-running local shell jobs. Waiting happens inside the MCP runtime: one `wait` call blocks until the real child process exits, so the model does not need to poll.

## Features

- Python 3.10+ and no third-party dependencies.
- `run_and_wait` for the recommended single-job workflow.
- Durable `run` and server-side `wait` with `all` / `any` modes.
- Bounded incremental logs through `output` and explicit process control through `kill`.
- SQLite job metadata and file-backed stdout/stderr logs that survive server restarts.
- Adaptive runtime estimates based on early progress samples.
- Review checkpoints that inspect output before stopping a slow job; healthy jobs are never killed by a hard timeout.
- Long-job handoff through `nohup_hours` (default: 3 hours). If the estimate exceeds the threshold, the call returns `status: "nohup"`, an estimated completion time, and a suggested next query time while the job keeps running.
- Bare `nohup` is blocked by default. When the user explicitly requests it, set `allow_manual_nohup: true` for MCP or append `# wait-mcp: user-nohup` to a shell command; other detached launchers remain blocked.

On Windows the handoff uses a native detached process. On Linux/macOS it uses a nohup-style independent session.

## Installation

No package installation is required:

```powershell
git clone https://github.com/438749902/wait-mcp.git
cd wait-mcp
python --version
python self_test.py
```

Configure Codex with absolute paths:

```toml
[mcp_servers.wait_mcp]
command = "D:/Python311/python.exe"
args = ["D:/src/wait-mcp/wait_mcp.py"]
startup_timeout_sec = 30
tool_timeout_sec = 86400
```

Restart Codex after changing the configuration. Set `WAIT_MCP_HOME` to change the data directory; the default is `%USERPROFILE%\\.codex-wait-mcp`.

## Recommended workflow

```json
{
  "command": ["python", "train.py", "--steps", "500"],
  "progress": {"total_steps": 500, "sample_steps": 10},
  "nohup_hours": 3
}
```

Make the job flush progress lines such as `step=10/500`, `epoch=2/20`, or `iteration=10/500`. The server samples the early output and estimates the total runtime:

1. If the estimate is below the threshold, the call keeps waiting.
2. At the predicted finish time, output is inspected and `review_required` is returned instead of killing the job.
3. If the estimated total runtime exceeds `nohup_hours`, the call returns the `nohup` handoff data. Use the returned `job_id` with `wait` later for the final result.

Without recognizable progress output, the server does not invent a runtime estimate or perform an automatic handoff.

Example of an explicitly authorized manual `nohup` MCP call:

```json
{"command": "nohup python train.py > train.log 2>&1 &", "allow_manual_nohup": true}
```

For the shell hook, append `# wait-mcp: user-nohup`. A bare `nohup` without that marker remains blocked.

## Tools

| Tool | Purpose |
| --- | --- |
| `run_and_wait` | Start and wait for one job; may return `nohup` for long jobs |
| `run` | Start a job and return a durable `job_id` |
| `wait` | Wait inside the server, with `all` / `any` modes |
| `output` | Read incremental stdout/stderr |
| `kill` | Stop a job after confirming failure or when explicitly required |
| `list` | Inspect durable jobs |

## Verification

```powershell
python self_test.py
python control_test.py
python list_test.py
python policy_test.py
python adaptive_progress_test.py
python acceptance.py
python acceptance_multi.py
python restart_test.py
```

## License

MIT. See [LICENSE](../LICENSE).
