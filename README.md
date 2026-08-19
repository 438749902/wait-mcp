# wait-mcp

面向 Windows、仅使用 Python 标准库的长时间本地 Shell 任务 MCP server。
Windows-first, stdlib-only MCP server for long-running local shell jobs.

核心特性是：`wait` 会在 MCP tool call 内持续阻塞，直到真实的子进程退出。
The key property is that `wait` blocks inside the MCP tool call until the real child process exits.

它不会向模型返回中间的“仍在运行”状态，因此不需要模型进行 polling。
It does not return an intermediate “still running” result, so the model does not need to poll.

阻塞等待的设计方式参考了 Reasonix，但本项目是独立实现。
The blocking-wait design references the Reasonix approach, but this project is an independent implementation.

## 功能 / What it provides

- `run`：启动命令并立即返回持久化的 `job_id`。
  `run`: start a command and immediately return a durable `job_id`.
- `wait`：等待选定的全部任务或任意一个任务完成。
  `wait`: block until all or any selected jobs finish.
- `output`：无需等待任务完成，即时读取有边界的增量输出。
  `output`: read bounded incremental output without waiting.
- `kill`：终止任务及其 Windows 进程树。
  `kill`: terminate a job and its Windows process tree.
- `list`：查看持久化任务元数据和状态。
  `list`: inspect durable job metadata and status.

任务存储在 SQLite 中，stdout/stderr 会实时写入文件，因此大量训练日志不会保留在 MCP 内存中。
Jobs are stored in SQLite and stdout/stderr are streamed to files, so large training logs are not retained in MCP memory.

默认数据目录是 `%USERPROFILE%\\.codex-wait-mcp`；设置 `WAIT_MCP_HOME` 可以修改该位置。
The default data directory is `%USERPROFILE%\\.codex-wait-mcp`; set `WAIT_MCP_HOME` to move it.

该 server 会以 MCP 进程的权限执行本地命令，请仅在可信的本地 Codex 环境中配置。
This server executes local commands with the permissions of the MCP process. Only configure it in a trusted local Codex environment.

## Windows 安装 / Install on Windows

不需要安装 Python 包或第三方依赖。克隆仓库后确认 Python 版本为 3.10 或更高。
No package or third-party dependency is required. Clone the repository and verify Python 3.10 or newer.

```powershell
git clone https://github.com/438749902/wait-mcp.git
cd wait-mcp
python --version
python self_test.py
```

在 Codex 配置中使用绝对 Python 路径和绝对 `wait_mcp.py` 路径。
Use an absolute Python path and an absolute `wait_mcp.py` path in Codex.

例如，编辑 `%USERPROFILE%\\.codex\\config.toml`：
For example, edit `%USERPROFILE%\\.codex\\config.toml`:

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

修改配置后重启 Codex。较长的 `tool_timeout_sec` 是运行数小时实验所必需的。
Restart Codex after changing the configuration. The long `tool_timeout_sec` is required for experiments that run for hours.

## 工具形状 / Tool shapes

```text
run(command: string | string[], cwd?: string, env?: object, name?: string)
wait(job_ids?: string[], mode?: "all" | "any")
output(job_id: string, tail_lines?: integer, offset?: {stdout?: integer, stderr?: integer})
kill(job_id: string, timeout_sec?: number)
list(status?: "running" | "completed" | "failed" | "killed")
```

`run` 会返回任务 ID、PID、状态、命令、工作目录和日志路径。
`run` returns the job id, PID, status, command, working directory, and log paths.

`wait` 会返回包含退出码、耗时、有限日志尾部和日志路径的完成记录，绝不会返回 polling 状态。
`wait` returns completion records with exit code, duration, bounded log tails, and log paths; it never returns a polling status.

服务器会并发处理 MCP 请求，因此一个 `wait` 阻塞时仍可处理 `kill` 请求。
The server processes MCP requests concurrently, so a `kill` request can be handled while another `wait` request is blocked.

服务器也支持标准 MCP cancellation notification（`notifications/cancelled` 和 `$/cancelRequest`）。取消 `wait` 会以错误码 `-32800` 释放 tool call，但不会终止实验；如果需要停止实验，请单独发送 `kill`。
It also accepts standard MCP cancellation notifications (`notifications/cancelled` and `$/cancelRequest`); cancelling `wait` releases the tool call with error code `-32800` but does not kill the experiment. Send `kill` separately when the experiment itself must stop.

在 tool call 仍在执行时，普通的新聊天消息无法被同一个 agent 分析。原因是模型正在等待当前 MCP 请求返回，Codex Desktop 不会把新消息自动转换成第二个 MCP 请求，也不会自动发送 cancellation notification；因此消息会排队，`wait` 仍会继续等待。
A plain new chat message cannot be analyzed by the same agent while its tool call is still in flight. The reason is that the model is waiting for the current MCP request to return, while Codex Desktop does not automatically turn the new message into a second MCP request or send a cancellation notification; the message is therefore queued and `wait` continues to wait.

如果需要调整实验或中断等待，请先在 Codex Desktop 中手动终止当前 tool call（Stop/Cancel），然后重新发送引导信息。模型重新获得控制权后，才能分析你的指令并决定调用 `kill`、`output`、`list` 或启动新的任务。
To adjust an experiment or interrupt a wait, manually stop or cancel the active tool call in Codex Desktop first, then send the guidance again. Once the model regains control, it can analyze the instruction and decide whether to call `kill`, `output`, `list`, or start a new job.

如果不希望打断当前 agent，也可以从另一个 Codex 任务或外部终端调用 `kill`、`output` 或 `list`；这些控制请求可以与阻塞中的 `wait` 并发处理。不要把 `wait` 改成定时返回“仍在运行”，否则会重新引入 polling 和中间模型采样。
If you do not want to interrupt the current agent, call `kill`, `output`, or `list` from another Codex task or an external terminal; these control requests can be handled concurrently with a blocked `wait`. Do not change `wait` to return periodic “still running” responses, because that would reintroduce polling and intermediate model sampling.

## 示例实验 / Dummy experiment

内置实验每秒输出一行并正常退出：
The included experiment emits one flushed line per second and exits zero:

```powershell
python dummy_experiment.py 120
```

在 Codex 中的预期调用流程：
The intended flow from Codex is:

```text
run({command: ["python", "dummy_experiment.py", "120"], name: "dummy-120"})
wait({job_ids: [job_id]})
output({job_id, tail_lines: 20})
```

MCP server 会持续停留在 `wait` 调用中，直到进程完成；只有一次性返回结果后，agent loop 才会继续。
The MCP server remains inside the `wait` call until the process completes; the agent loop can continue only after the single result is returned.

## 验证 / Checks

```powershell
python self_test.py
python control_test.py          # concurrent kill and MCP cancellation
python list_test.py             # list while wait is blocked
python acceptance.py            # 120-second blocking wait
python acceptance_multi.py      # output, kill, any/all, list
python restart_test.py          # durable registry recovery check
```

验收脚本会把 JSON trace 写入仓库同级的 `outputs` 目录。
The acceptance scripts write JSON traces under the repository's sibling `outputs` directory.

120 秒测试只发送一次 MCP 请求并在该请求内阻塞，因此记录到 0 次中间模型采样和 0 次重复 wait 调用。
The 120-second trace records zero intermediate model sampling and zero repeated wait calls because the test drives one MCP request and blocks on that request.

## 许可证 / License

MIT，详见 [LICENSE](LICENSE)。
MIT. See [LICENSE](LICENSE).

