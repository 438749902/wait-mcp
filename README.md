# wait-mcp

Windows-first, stdlib-only MCP server for long-running local shell jobs.

The important property is that `wait` blocks inside the MCP tool call until the
real child process exits. It does not return an intermediate “still running”
result for the model to poll. The blocking-wait design references the Reasonix
approach, but this project is an independent implementation.

## What it provides

- `run`: start a command and immediately return a durable `job_id`.
- `wait`: block until all or any selected jobs finish.
- `output`: read bounded incremental output without waiting.
- `kill`: terminate a job and its Windows process tree.
- `list`: inspect durable job metadata and status.

Jobs are stored in SQLite and stdout/stderr are streamed to files, so large
training logs are not retained in MCP memory. The default data directory is
`%USERPROFILE%\\.codex-wait-mcp`; set `WAIT_MCP_HOME` to move it.

This server executes local commands with the permissions of the MCP process.
Only configure it in a trusted local Codex environment.

## Install on Windows

No package or third-party dependency is required. Clone the repository and
verify Python 3.10 or newer:

```powershell
git clone https://github.com/43874902/wait-mcp.git
cd wait-mcp
python --version
python self_test.py
```

Use an absolute Python path and an absolute `wait_mcp.py` path in Codex. For
example, edit `%USERPROFILE%\\.codex\\config.toml`:

```toml
[mcp_servers.wait_mcp]
command = "D:/Python311/python.exe"
args = ["D:/src/wait-mcp/wait_mcp.py"]
startup_timeout_sec = 30
tool_timeout_sec = 86400

[features.code_mode]
enabled = true
direct_only_tool_namespaces = ["mcp__wait_mcp"]
```

Restart Codex after changing the configuration. The long `tool_timeout_sec`
is required for experiments that run for hours.

## Tool shapes

```text
run(command: string | string[], cwd?: string, env?: object, name?: string)
wait(job_ids?: string[], mode?: "all" | "any")
output(job_id: string, tail_lines?: integer, offset?: {stdout?: integer, stderr?: integer})
kill(job_id: string, timeout_sec?: number)
list(status?: "running" | "completed" | "failed" | "killed")
```

`run` returns the job id, PID, status, command, working directory, and log
paths. `wait` returns completion records with exit code, duration, bounded log
tails, and log paths. It never returns a polling status.

The server processes MCP requests concurrently, so a `kill` request can be
handled while another `wait` request is blocked. It also accepts the standard
MCP cancellation notifications (`notifications/cancelled` and
`$/cancelRequest`); cancelling `wait` releases the tool call with error code
`-32800` but does not kill the experiment. Send `kill` separately when the
experiment itself must stop.

A plain new chat message cannot be analyzed by the same agent while its tool
call is still in flight. Codex must cancel the active tool call when the user
interrupts; otherwise the message remains queued until `wait` returns. This is
an agent-runtime constraint, not a reason to replace real blocking with model
polling.

## Dummy experiment

The included experiment emits one flushed line per second and exits zero:

```powershell
python dummy_experiment.py 120
```

From Codex, the intended flow is:

```text
run({command: ["python", "dummy_experiment.py", "120"], name: "dummy-120"})
wait({job_ids: [job_id]})
output({job_id, tail_lines: 20})
```

The MCP server remains inside the `wait` call until the process completes; the
agent loop can continue only after the single result is returned.

## Checks

```powershell
python self_test.py
python control_test.py          # concurrent kill and MCP cancellation
python list_test.py             # list while wait is blocked
python acceptance.py          # 120-second blocking wait
python acceptance_multi.py    # output, kill, any/all, list
python restart_test.py        # durable registry recovery check
```

The acceptance scripts write JSON traces under the repository's sibling
`outputs` directory. The 120-second trace records zero intermediate model
sampling and zero repeated wait calls because the test drives one MCP request
and blocks on that request.

## License

MIT. See [LICENSE](LICENSE).
