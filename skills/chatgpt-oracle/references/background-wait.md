# 用后台进程等待 Oracle

ChatGPT 与 Gemini 的任务／导出等待都适用。连接方式见 [connection.md](connection.md)，发送身份见 [task-identity.md](task-identity.md)，Gemini 的状态和取回边界见 [gemini.md](gemini.md)。

## 默认组织方式

提交一次并保存回执后，将轮询逻辑一次性写成持续运行的 shell／脚本，由客户端 harness 的后台执行机制启动。后台进程自行调用已有 MCP 客户端、sleep、重连和落盘，不需要模型每隔几秒或几十秒再生成一次 Python / curl / MCP 查询。多个已提交任务可以由同一个监控进程维护各自身份与截止时间。

优先使用 harness 自带的后台任务及完成通知。主模型继续处理独立工作，收到“完成”“需要介入”或“等待失败”的通知时再读取结果。流式 progress 文案变化只更新本地状态，不反复唤醒模型或向用户播报片段。没有完成通知时，保留后台 PID 和状态文件，在确实需要结果时读取，避免为固定间隔检查额外发起模型轮次。

## 后台脚本的职责

1. 固定提供方、MCP 入口、`task_id`、`session_id`、`request_id` 和已有 URL。使用客户端已经配置好的 MCP 连接及凭据；不把密码或 Bearer 放进启动参数、日志或状态文件。
2. 问答只调用对应的 `chatgpt_status` / `gemini_status`；导出使用对应的 `*_export_status` 并保留 `session_id` / `export_id`。按回执的 `retry_after_seconds` / Retry-After 等待；无提示时可用普通问答 15–30 秒、研究 60–180 秒的间隔，按实际延迟调整。这些间隔由进程内部计时。
3. 有总截止时间与有限的网络错误退避；保留最近一次成功状态。临时断线仅重连并查询同一任务，不重新 submit、resume 或启动研究。持续错误／截止时间到时记录“等待未完成”，不能推断远端任务已失败、取消或清理完毕。
4. 原子更新小型状态文件，如 `status.json`；普通日志只在状态或错误改变时追加，避免将完整流式 progress 不断注入模型上下文。
5. 按下面的停止条件退出。后台只负责观察和已完成结果的取回，不自动决定续问、切换模型、降档、取消或关闭 Session。

| 状态 | 后台处理 |
| --- | --- |
| `QUEUED`、`STARTING`、`UPLOADING`、`ATTACHMENTS_VERIFIED`、`SEND_PENDING`、`RUNNING` | 保留身份，按间隔继续等待 |
| `CANCEL_REQUESTED` | 等待取消确认；不视为已取消或已释放 |
| `COMPLETE` | 调用一次对应 result 入口；大结果按回执分页到 EOF，重组核对长度和 SHA-256，保存正文与核验摘要后通知 Main |
| `NEEDS_USER_INPUT` | 保存 `user_input_kind`、`clarification_text` 与 instructions，通知 Main 并退出；由 Main 判断是否需要人类 |
| `ERROR / ACCOUNT_CONTEXT_REQUIRED` | 先于一般 ERROR 分支处理：保存 `account_switch`、`account_attempts` 和原身份，通知 Main 重建上下文后建立新 Session；后台不自动重发，也不要求用户维护网页 |
| `ERROR`、`TIMEOUT`、`MODEL_UNAVAILABLE`、`CANCELLED` | 保存原回执及错误，通知 Main 并退出；不重发原问题 |
| 导出 `READY` | 按授权范围取回快照并核验；READY / EOF 不替代历史完整性或实际阅读证据 |
| 未识别状态、无法解释的响应或本地等待超时 | 保留现场，交回 Main；不无限循环 |

`SEND_PENDING / unconfirmed` 可能是在等待上游建立正式会话，并非终态故障；继续观察原 task。客户端监控截止时间应考虑服务的任务等待预算，不能把本地 watcher 退出当作上游任务失败或原问题未发送。

从 service capabilities 的 `wait_budgets_seconds` 读取观察预算。已确认任务的 `GENERATION_WAIT_TIMEOUT`／`TURN_RESULT_WAIT_TIMEOUT` 仍按 TIMEOUT 保存并交 Main，不能推出上游已停。另行查询浏览器能力得到可重试 `BUSY / BROWSER_DIAGNOSTIC_BUSY` 时，保留原任务监控；它只是诊断容量不足，按退避稍后重试，不把该查询结果写成原任务失败。

服务端在原任务内进行运行时恢复时，后台继续查询同一 task；不在客户端另写刷新、重试发送或改换身份的恢复循环。只有实际登录／人工 CAPTCHA 才交用户；恢复耗尽的服务错误交 Main 排查，不把常规运行时维护转成用户操作。

结果取回中断时保留 `result_id` / 游标，恢复只读传输；哈希核验失败则记录传输失败，不能把残缺正文标为成功。任务完成与本地结果完整是两个状态，分别记录。

## 启动示例

客户端先按上述职责生成本次任务的 `oracle-watch.sh` 或 `oracle-watch.ps1`。以下只演示启动方式，脚本由客户端结合已有 MCP 客户端实现，并非 Skill 随包脚本。

有 harness 后台工具时直接用它运行脚本并接收退出通知。自行启动的 Bash 示例：

```bash
nohup bash ./oracle-watch.sh >./oracle-watch.log 2>&1 &
watch_pid=$!
printf '%s\n' "$watch_pid" >./oracle-watch.pid
```

Windows 自行启动时使用隐藏窗口：

```powershell
$watchProcess = Start-Process -FilePath 'pwsh.exe' `
    -ArgumentList @('-NoProfile', '-File', './oracle-watch.ps1') `
    -WindowStyle Hidden `
    -RedirectStandardOutput './oracle-watch.log' `
    -RedirectStandardError './oracle-watch.err.log' -PassThru
$watchProcess.Id | Set-Content -LiteralPath './oracle-watch.pid'
```

给每次监控使用独立目录或文件名，保存 PID／harness task handle，退出后清理自身资源。停止本地 watcher 不等于取消远端 Oracle 任务；仍要按原身份核查，需要取消时由 Main 使用对应工具。
