# wait-mcp

[English README](doc/README.md)

面向 Codex 的本地长任务 MCP server。它把等待动作放进 MCP runtime：一次 `wait` 调用在服务端阻塞，直到真实子进程结束，避免模型反复 polling。

## 特性

- 仅使用 Python 标准库，Windows 优先，Python 3.10+。
- `run_and_wait`：启动并等待单个任务，推荐用于训练和实验。
- `run`、`wait`：支持持久化任务，以及 `all` / `any` 并发等待。
- `output`：读取有界的增量 stdout/stderr；`kill`：优雅终止并按需清理进程树。
- SQLite 保存任务元数据，日志实时写入文件，服务重启后可恢复任务状态。
- 自适应耗时估计：根据早期进度计算平均步时和预计完成时间。
- 安全检查点：预计时间到达后先分析 stdout/stderr，健康任务不会被硬超时误杀；异常时返回 `review_required`。
- 长任务交接：预计时长超过 `nohup_hours`（默认 3 小时）时返回 `status: "nohup"`、预计完成时间和下次建议查询时间，任务继续运行。
- 默认禁止裸用 `nohup`；用户明确要求时，MCP 设置 `allow_manual_nohup: true`，Shell 命令追加 `# wait-mcp: user-nohup`。其他脱离式启动仍被禁止。

Windows 使用原生 detached 进程实现上述后台交接；Linux/macOS 使用 nohup 风格的独立会话。

## 安装

不需要安装依赖：

```powershell
git clone https://github.com/438749902/wait-mcp.git
cd wait-mcp
python --version
python self_test.py
```

在 Codex 配置中使用绝对路径：

```toml
[mcp_servers.wait_mcp]
command = "D:/Python311/python.exe"
args = ["D:/src/wait-mcp/wait_mcp.py"]
startup_timeout_sec = 30
tool_timeout_sec = 86400
```

修改配置后重启 Codex。可通过 `WAIT_MCP_HOME` 修改默认数据目录；默认目录为 `%USERPROFILE%\\.codex-wait-mcp`。

## 推荐调用方式

```json
{
  "command": ["python", "train.py", "--steps", "500"],
  "progress": {"total_steps": 500, "sample_steps": 10},
  "nohup_hours": 3
}
```

让任务输出类似 `step=10/500`、`epoch=2/20` 或 `iteration=10/500` 的已刷新进度行。服务端先采样早期进度，再估算总时长：

1. 预计时间未超过阈值：继续阻塞等待。
2. 预计时间到达：读取输出并返回 `review_required`，不自动杀死任务。
3. 预计总时长超过 `nohup_hours`：返回 `nohup` 交接信息，之后用返回的 `job_id` 调用 `wait` 获取最终结果。

没有可识别的进度输出时，服务端不会伪造耗时估计，也不会自动交接。

用户明确指定手动 `nohup` 时，MCP 调用示例：

```json
{"command": "nohup python train.py > train.log 2>&1 &", "allow_manual_nohup": true}
```

通过 Shell hook 时，在命令末尾追加 `# wait-mcp: user-nohup`；没有该标记的裸 `nohup` 仍会被拦截。

## 工具

| 工具 | 用途 |
| --- | --- |
| `run_and_wait` | 启动并等待一个任务；长任务可能返回 `nohup` |
| `run` | 启动任务并立即返回 `job_id` |
| `wait` | 在服务端等待任务完成，支持 `all` / `any` |
| `output` | 增量读取 stdout/stderr |
| `kill` | 终止任务；仅在确认失败或明确需要停止时使用 |
| `list` | 查看持久化任务 |

## 验证

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

## 许可

MIT，详见 [LICENSE](LICENSE)。
