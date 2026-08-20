# wait-mcp

面向 Windows、仅使用 Python 标准库的长时间本地 Shell 任务 MCP server。<br>
Windows-first, stdlib-only MCP server for long-running local shell jobs.

## 问题背景

在原有的 Codex 执行路径中，长时间任务可能被处理成“查询—返回—再次查询”的循环；典型表现是大约每 30 秒重新查询一次任务状态。<br>
In the original Codex execution path, long-running jobs could be handled as a “query, return, query again” loop; a typical symptom was another status query roughly every 30 seconds.

对于运行数小时的训练或实验，这会产生大量无意义的中间模型调用，持续消耗 token，并把重复的任务状态、日志片段和工具结果堆积到上下文中。<br>
For training runs or experiments lasting several hours, this creates many unnecessary intermediate model calls, consumes tokens continuously, and fills the context with repeated job states, log fragments, and tool results.

这种 polling 还会让 LLM 被迫承担 scheduler 的职责：它需要反复确认任务是否完成，而不是在任务结束后一次性处理结果。<br>
This polling also makes the LLM act as a scheduler: it repeatedly checks whether the job has finished instead of processing one final result when the job completes.

## 解决方案

`wait-mcp` 将等待移动到确定性的 MCP runtime 中：一次 `wait` 调用会在服务器内部真正阻塞，直到目标进程完成，然后一次性返回结果。<br>
`wait-mcp` moves waiting into deterministic MCP runtime: one `wait` call blocks inside the server until the target process completes, then returns the result once.

因此，长时间任务运行期间不会产生重复的 `wait` 调用、周期性“仍在运行”响应或中间模型采样。<br>
As a result, long-running jobs produce no repeated `wait` calls, periodic “still running” responses, or intermediate model sampling while they are running.

P0 目标是：长任务运行期间 0 次 polling、0 次重复 wait 调用、0 次中间模型采样；任务完成后，agent loop 再继续分析结果。<br>
The P0 target is: zero polling, zero repeated wait calls, and zero intermediate model sampling while a long-running job executes; the agent loop resumes only after the job completes.

## 项目简介

`wait-mcp` 用于运行 Python 训练、CUDA/PyTorch 实验和其他需要长时间执行的本地任务。
`wait-mcp` runs local tasks that may take a long time, including Python training jobs and CUDA/PyTorch experiments.

核心设计是：`wait` 会在 MCP tool call 内真正阻塞，直到真实的子进程结束。<br>
The core design is that `wait` blocks inside the MCP tool call until the real child process exits.

它不会返回“仍在运行”的中间结果，因此不需要模型进行周期性 polling 或重复采样。<br>
It does not return intermediate “still running” results, so the model does not need periodic polling or repeated sampling.

本项目参考 Reasonix 的设计方式开发；核心 blocking-wait 思路并非本项目原创，感谢 Reasonix 提供设计参考。<br>
This project was developed with reference to Reasonix's design. The core blocking-wait idea is not original to this project; Reasonix is credited as the design reference.

> **核心原则**<br>
> LLM 负责提出假设、启动实验、等待结果和解释结果；等待本身由确定性的运行时完成。<br>
> **Core principle**<br>
> The LLM proposes hypotheses, launches experiments, waits for results, and interprets them; deterministic runtime handles the waiting.

## 功能

### 推荐执行路径 / Recommended execution path

单个长时间实验优先调用 `run_and_wait`：它在同一个 MCP tool call 中启动并等待到任务结束，Codex 不需要自己记住第二步，也不会自然退回 `nohup`。<br>
For one long-running experiment, prefer `run_and_wait`: it starts and waits in one MCP tool call, so Codex does not have to remember a second step or fall back to `nohup`.

只有明确需要并行运行多个实验时，才使用 `run`；返回 `job_id` 后下一步必须立即调用 `wait`，再进行其他分析或 shell 操作。<br>
Use `run` only when explicit concurrency is needed; after it returns a `job_id`, the next action must be `wait` before other analysis or shell work.

当前 Codex Desktop 仍允许模型直接调用普通 shell，因此提示词本身不能形成硬保证。本项目的 MCP server instructions 会把上述流程注入工具上下文；安装附带的 Codex hook 后，还可以拦截 `nohup`、`Start-Process` 等脱离式后台启动命令。<br>
Current Codex Desktop still allows the model to call ordinary shell tools, so prompting alone is not a hard guarantee. This server injects the workflow through MCP server instructions; the optional Codex hook can additionally block detached launch commands such as `nohup` and `Start-Process`.

仓库中的 `wait_mcp_policy.py` 是该 hook 的最小标准库实现；它只拦截脱离式后台启动，不拦截普通同步 shell 命令。<br>
The repository's `wait_mcp_policy.py` is the minimal stdlib implementation of that hook; it blocks only detached background launches and does not block ordinary synchronous shell commands.

### `run`

启动命令并立即返回持久化的 `job_id`。<br>
Start a command and immediately return a durable `job_id`.

### `run_and_wait`

单个实验的推荐入口：启动命令后在 MCP 内部阻塞，直到完成再返回最终结果。<br>
The recommended entry point for one experiment: start the command, block inside MCP, and return only the final result.

### `wait`

在 MCP server 内阻塞，等待全部任务或任意一个任务完成。<br>
Block inside the MCP server until all or any selected jobs finish.

### `output`

无需等待任务完成，即时读取有边界的增量输出。<br>
Read bounded incremental output without waiting for the job to finish.

### `kill`

优雅终止任务，必要时强杀其 Windows 进程树。<br>
Gracefully terminate a job and force-kill its Windows process tree when necessary.

### `list`

查看持久化任务的元数据和状态。<br>
Inspect durable job metadata and status.

任务存储在 SQLite 中，stdout/stderr 会实时写入文件，因此大量训练日志不会保留在 MCP 内存中。<br>
Jobs are stored in SQLite and stdout/stderr are streamed to files, so large training logs are not retained in MCP memory.

默认数据目录是 `%USERPROFILE%\\.codex-wait-mcp`；设置 `WAIT_MCP_HOME` 可以修改该位置。<br>
The default data directory is `%USERPROFILE%\\.codex-wait-mcp`; set `WAIT_MCP_HOME` to move it.

## Windows 安装

不需要安装 Python 包或第三方依赖。克隆仓库后确认 Python 版本为 3.10 或更高。<br>
No package or third-party dependency is required. Clone the repository and verify Python 3.10 or newer.

```powershell
git clone https://github.com/438749902/wait-mcp.git
cd wait-mcp
python --version
python self_test.py
```

在 Codex 配置中使用绝对 Python 路径和绝对 `wait_mcp.py` 路径。<br>
Use an absolute Python path and an absolute `wait_mcp.py` path in Codex.

例如，编辑 `%USERPROFILE%\\.codex\\config.toml`：<br>
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

修改配置后重启 Codex。较长的 `tool_timeout_sec` 是运行数小时实验所必需的。<br>
Restart Codex after changing the configuration. The long `tool_timeout_sec` is required for experiments that run for hours.

### 安装全局 hook / Install the global hook

当前 Codex schema 会从 `%USERPROFILE%\\.codex\\hooks.json` 或 `config.toml` 读取 hook。推荐使用独立的 `hooks.json`；把下面内容保存到该路径，并替换 Python 与脚本路径。<br>
The current Codex schema loads hooks from `%USERPROFILE%\\.codex\\hooks.json` or `config.toml`. Prefer a separate `hooks.json`; save the following there and replace the Python and script paths.

```json
{
  "description": "wait-mcp detached experiment launch guard",
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "^Bash$",
        "hooks": [
          {
            "type": "command",
            "command_windows": "D:/miniconda/envs/py311/python.exe C:/path/to/wait-mcp/wait_mcp_policy.py",
            "timeout": 30,
            "statusMessage": "Checking detached experiment launch"
          }
        ]
      }
    ]
  }
}
```

重启 Codex 后打开 `/hooks`，review 并 trust 这个非托管 hook；否则它会出现在列表中但不会执行。<br>
After restarting Codex, open `/hooks` and review and trust this non-managed hook; otherwise it may appear in the list but will not run.

这只是安全护栏；真正的默认行为由 `run_and_wait`、MCP `instructions` 和全局 `AGENTS.md` 共同决定。修改全局配置后必须重启 Codex Desktop。<br>
This is a guardrail; the default workflow comes from `run_and_wait`, MCP `instructions`, and global `AGENTS.md` together. Restart Codex Desktop after changing global configuration.

## 工具 schema

```text
run(command: string | string[], cwd?: string, env?: object, name?: string, nohup_hours?: number)
run_and_wait(command: string | string[], cwd?: string, env?: object, name?: string, nohup_hours?: number)
wait(job_ids?: string[], mode?: "all" | "any")
output(job_id: string, tail_lines?: integer, offset?: {stdout?: integer, stderr?: integer})
kill(job_id: string, timeout_sec?: number)
list(status?: "running" | "completed" | "failed" | "killed", cwd?: string)
```

`run` 会返回任务 ID、PID、状态、命令、工作目录和日志路径。<br>
`run` returns the job id, PID, status, command, working directory, and log paths.

`wait` 会返回退出码、耗时、有限日志尾部和日志路径，绝不会返回 polling 状态。<br>
`wait` returns the exit code, duration, bounded log tails, and log paths; it never returns a polling status.

`run_and_wait` 通常返回完成任务对象；如果预计时长超过 `nohup_hours`（默认 3 小时），则返回 `status: "nohup"`、预计完成时间和下次建议查询时间，任务继续在脱离式进程组中运行。<br>
`run_and_wait` normally returns one completed job object. If the estimate exceeds `nohup_hours` (default 3 hours), it returns `status: "nohup"` with an estimated completion time and next query time while the detached job continues running.

通过 MCP 调用启动的脱离式命令会被拒绝；直接 shell 调用不经过本 server，必须由 Codex hook 或全局规则另行拦截。<br>
Detached commands launched through MCP are rejected; direct shell calls bypass this server and require a separate Codex hook or global rule.

服务器会并发处理 MCP 请求，因此一个 `wait` 阻塞时仍可处理 `kill`、`output` 和 `list`。<br>
The server processes MCP requests concurrently, so `kill`, `output`, and `list` can be handled while another `wait` is blocked.

服务器支持标准 MCP cancellation notification：`notifications/cancelled` 和 `$/cancelRequest`。取消 `wait` 会以错误码 `-32800` 释放 tool call，但不会终止实验；如果需要停止实验，请单独发送 `kill`。<br>
The server supports standard MCP cancellation notifications: `notifications/cancelled` and `$/cancelRequest`. Cancelling `wait` releases the tool call with error code `-32800` but does not kill the experiment; send `kill` separately when the experiment itself must stop.

## 等待期间的输入引导

普通的新聊天消息无法在同一个 agent 的 MCP tool call 仍执行时被分析。原因是模型正在等待当前 MCP 请求返回，而 Codex Desktop 不会把新消息自动转换成第二个 MCP 请求，也不会自动发送 cancellation notification；因此消息会排队，`wait` 仍会继续等待。<br>
A plain new chat message cannot be analyzed while the same agent's MCP tool call is still in flight. The model is waiting for the current MCP request to return, while Codex Desktop does not automatically turn the new message into a second MCP request or send a cancellation notification; the message is queued and `wait` continues to wait.

如果需要调整实验或中断等待，请先在 Codex Desktop 中手动点击 Stop/Cancel，终止当前 tool call，然后重新发送引导信息。<br>
To adjust an experiment or interrupt a wait, manually click Stop/Cancel in Codex Desktop to end the active tool call, then send the guidance again.

模型重新获得控制权后，才能分析你的指令并决定调用 `kill`、`output`、`list` 或启动新的任务。<br>
Once the model regains control, it can analyze the instruction and decide whether to call `kill`, `output`, `list`, or start a new job.

如果不希望打断当前 agent，也可以从另一个 Codex 任务或外部终端调用 `kill`、`output` 或 `list`；这些控制请求可以与阻塞中的 `wait` 并发处理。<br>
If you do not want to interrupt the current agent, call `kill`, `output`, or `list` from another Codex task or an external terminal; these control requests can be handled concurrently with a blocked `wait`.

不要把 `wait` 改成定时返回“仍在运行”，否则会重新引入 polling 和中间模型采样。<br>
Do not change `wait` to return periodic “still running” responses, because that would reintroduce polling and intermediate model sampling.

## 示例实验

内置实验每秒输出一行并正常退出。<br>
The included experiment emits one flushed line per second and exits zero.

```powershell
python dummy_experiment.py 120
```

在 Codex 中的预期调用流程：<br>
The intended flow from Codex:

```text
run({command: ["python", "dummy_experiment.py", "120"], name: "dummy-120"})
wait({job_ids: [job_id]})
output({job_id, tail_lines: 20})
```

MCP server 会持续停留在 `wait` 调用中，直到进程完成；只有一次性返回结果后，agent loop 才会继续。<br>
The MCP server remains inside the `wait` call until the process completes; the agent loop continues only after the single result is returned.

## 验证

```powershell
python self_test.py
python control_test.py          # concurrent kill and MCP cancellation
python list_test.py             # list while wait is blocked
python acceptance.py            # 120-second blocking wait
python acceptance_multi.py      # output, kill, any/all, list
python restart_test.py          # durable registry recovery check
```

验收脚本会把 JSON trace 写入仓库同级的 `outputs` 目录。<br>
The acceptance scripts write JSON traces under the repository's sibling `outputs` directory.

120 秒测试只发送一次 MCP 请求并在该请求内阻塞，因此记录到 0 次中间模型采样和 0 次重复 wait 调用。<br>
The 120-second trace records zero intermediate model sampling and zero repeated wait calls because the test drives one MCP request and blocks on that request.

## 安全提醒

该 server 会以 MCP 进程的权限执行本地命令，请仅在可信的本地 Codex 环境中配置。<br>
This server executes local commands with the permissions of the MCP process. Only configure it in a trusted local Codex environment.

## 许可证

MIT，详见 [LICENSE](LICENSE)。<br>
MIT. See [LICENSE](LICENSE).

## Adaptive experiment timing

Pass `progress` for experiments that report progress, for example:

```json
{"total_steps": 500, "sample_steps": 10}
```

The server samples the flushed stdout progress, estimates average step time and total duration, and checks output at the predicted finish time. It never terminates a healthy job merely because the estimate was exceeded: it returns `review_required` with stdout/stderr diagnostics, and the caller can resume with `wait` or terminate with `kill` after confirming a failure. The default pattern recognizes `step`, `epoch`, `round`, `iteration`, and `iter` output such as `step=10/500`; `total_steps` may be omitted when the output contains `/total`.

The result exposes `progress.estimated_duration_sec`, `progress.avg_step_sec`, `progress.checkpoint_at`, and `review.fatal_signals`. A slow but healthy run remains alive; only an explicit `kill` or a real process exit ends it. Long jobs also return `nohup.estimated_completion_at` and `nohup.next_query_at`.
