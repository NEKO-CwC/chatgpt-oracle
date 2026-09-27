# chatgpt-oracle

本地的 ChatGPT Web 外部研究桥接器。它把 ChatGPT Web 当作一个异步的咨询、研究和批评角色，向 Codex 暴露语义级 MCP 工具。浏览器 worker 只连接本机 `127.0.0.1` 的正式 Chrome CDP，不把 CDP 暴露给 MCP 或 FRP，不读取 Cookie。

## 运行方式

项目使用 Windows Python 3.11+。首次准备：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

初始化专用浏览器 Profile：

```powershell
$env:CHATGPT_ORACLE_ROOT = (Get-Location).Path
$env:CHATGPT_BROWSER_HEADLESS = "0"
.\.venv\Scripts\python.exe -m chatgpt_oracle.cli browser-setup
```

窗口中完成 ChatGPT 登录后按 Enter。Profile 默认位于 `runtime/chatgpt-browser-profile`，不会复用或读取日常 Chrome Profile。

本地 stdio MCP：

```powershell
$env:CHATGPT_ORACLE_TRANSPORT = "stdio"
.\.venv\Scripts\python.exe -m chatgpt_oracle.server
```

本机 HTTP MCP：

```powershell
$env:CHATGPT_ORACLE_TRANSPORT = "http"
$env:CHATGPT_ORACLE_HOST = "127.0.0.1"
$env:CHATGPT_ORACLE_PORT = "8787"
$env:CHATGPT_ORACLE_ALLOWED_HOSTS = "chatgpt-oracle.frp-service.neko-dashboard.com:32751"
$env:CHATGPT_ORACLE_BEARER_TOKEN = [Environment]::GetEnvironmentVariable('CHATGPT_ORACLE_BEARER_TOKEN', 'User')
if (-not $env:CHATGPT_ORACLE_BEARER_TOKEN) { throw "Set CHATGPT_ORACLE_BEARER_TOKEN in the current user's environment first." }
.\.venv\Scripts\python.exe -m chatgpt_oracle.server
```

文件直传接口：

```text
POST http://127.0.0.1:8787/uploads
Authorization: Bearer <token>
X-Upload-Filename: notes.txt
Content-Type: text/plain
<raw request body>
```

旧接口返回短期 `upload_id`，使用服务端内存暂存，兼容小文件。大文件优先使用下面的 tus 接口；
它把字节暂存在独立临时目录，并把文件内容与任务/SessionStore/审计日志分开。
默认单文件上限为 25 MiB、总内存上限为 64 MiB、最多 8 个待使用文件，分别可通过
`CHATGPT_UPLOAD_MAX_FILE_BYTES`、`CHATGPT_UPLOAD_MAX_TOTAL_BYTES` 和
`CHATGPT_UPLOAD_MAX_FILES` 调整。

本机 Streamable HTTP MCP 地址为 `http://127.0.0.1:8787/mcp`，健康检查为 `/healthz`。当前公网 MCP 地址为 `https://chatgpt-oracle.frp-service.neko-dashboard.com:32751/mcp`。通过 FRP 暴露时，将 `CHATGPT_ORACLE_ALLOWED_HOSTS` 设置为 FRP 请求使用的精确 Host，并把外部入口转发到该 MCP 端口；不要转发 Chrome CDP 端口。

如需注册 Windows 登录后自动启动的 HTTP Broker：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\register-http-mcp-task.ps1 -AllowedHosts 'chatgpt-oracle.frp-service.neko-dashboard.com:32751'
```

注册脚本会在当前 Windows 用户环境中生成并保存远程访问用的 Bearer Token。客户端应使用 `Authorization: Bearer <token>`；优先通过客户端的安全凭据存储或受支持的环境变量引用配置，不要把 token 提交到仓库或日志。

```powershell
$env:CHATGPT_ORACLE_BEARER_TOKEN = [Environment]::GetEnvironmentVariable('CHATGPT_ORACLE_BEARER_TOKEN', 'User')
```

## MCP 工具

- `chatgpt_submit(mode, prompt, project?, model?, quality?, reasoning_effort?, allow_downgrade?, on_busy?, request_id?, upload_ids?, files?, session_id?)`：创建任务并选择模型/能力；传入新的 `session_id` 会为该逻辑会话绑定一个独立 ChatGPT 标签页。`upload_ids` 是旧内存上传入口；`files=[{"file_ref": "..."}]` 可把已验证文件交给该 Session 的浏览器上传。
- `chatgpt_status(task_id)`：返回任务状态，不阻塞等待。
- `chatgpt_result(task_id, delivery="legacy"|"inline"|"reference")`：默认保留原内联响应；`reference` 返回稳定的 `result_id`，可用于逐页读取。显式 `inline` 超过配置响应大小时返回 `RESULT_TOO_LARGE`，并给出分页入口。
- `chatgpt_result_page(session_id, result_id, cursor?, max_response_bytes=8192)`：读取带 SHA-256 和 EOF 标记的 UTF-8 结果页。文件结果游标和会话导出游标互不通用。
- `chatgpt_export_start(session_id?, task_id?, conversation_url?, scope="all"|"recent", count=10, request_id?, on_busy?)`：开始一次 session 绑定的不可变导出快照，立即返回 `session_id`、`export_id` 和排队状态。相同 request_id 在同一 session 下幂等；输入不同返回 `IDEMPOTENCY_CONFLICT`。`count` 只约束 `recent` 的 user 回合数，不限制 `all` 的源 API 页数。
- `chatgpt_export_status(session_id, export_id)`：返回 QUEUED/RUNNING/CANCEL_REQUESTED/CANCELLED/READY/FAILED/NEEDS_USER_INPUT/GONE/RELEASED、消息与附件分项完整性、已处理分页请求计数、各视图字节数和 SHA-256。READY 只表示快照可读，不证明历史完整。
- `chatgpt_export_page(session_id, export_id, view, cursor?, max_response_bytes=8192)`：读取 `messages`、`attachments` 或 `markdown` 的有界 UTF-8 分页。响应上限 16 KiB；相同游标可安全重试；不因页数达到固定次数而提前拒绝，末页返回 `eof=true` 和空 `next_cursor`。
- `chatgpt_export_cancel(session_id, export_id)`：取消排队或运行中的导出；运行中会中断当前浏览器读取并等待槽位释放。若暂未释放，会返回 `CANCEL_REQUESTED`，可继续查状态。
- `chatgpt_export_attachment_handle(session_id, export_id, attachment_id)`：为 manifest 中标记 `available` 或 `download_candidate` 的附件领取短期句柄。candidate 表示要在隔离浏览器页尝试 ChatGPT 文件卡片动作，不代表已确认可下载。用 Bearer 认证 GET 返回的 `download_url`；响应包含 `Content-Length` 与 `X-Content-SHA256`，客户端应核对后才确认传输完成。失败后可重新领取句柄并报告 unavailable。
- `chatgpt_export_release(session_id, export_id)`：只释放 READY 快照；QUEUED/RUNNING 必须使用 `chatgpt_export_cancel`。未释放的快照会由后台维护任务按 TTL 清理并留下 GONE 审计事件。
- `chatgpt_file_upload_start(session_id, filename, size_bytes, mime_type?, sha256?, request_id?)`：返回 tus 1.0.0 Creation endpoint、metadata 和当前限额；用 tus 客户端执行 POST，完成的 Location 保存为 `transfer_id`。
- `chatgpt_transfer_status(session_id, transfer_id)` / `chatgpt_transfer_cancel(session_id, transfer_id)`：查询可恢复上传或附件准备任务的阶段、偏移、错误和完成的 `file_ref`，或取消未完成操作。
- `chatgpt_file_download_start(session_id, export_id, attachment_id, request_id?)`：按 manifest 的具体附件开始源文件准备。返回 `FETCHING` 后查询 transfer status；READY 才有可复用的下载信息。文件完整读取支持 `HEAD` 和单段 Range。
- `chatgpt_file_info(session_id, file_ref)` / `chatgpt_file_release(session_id, file_ref)`：查询文件 MIME、实际大小、表示来源、完整 SHA-256 和下载路径，或释放 Session 内引用。下载路径通过 Range 流式读取，并由 Bearer 与 Session 共同校验。
- `chatgpt_export(task_id?, session_id?, conversation_url?, scope="recent"|"all", count=10, request_id?, on_busy?)`：旧版 inline 兼容工具；完整会话归档请使用上述分页工具，避免大响应被 MCP 客户端截断。
- `chatgpt_continue(task_id, prompt, quality?, model?, reasoning_effort?, allow_downgrade?, on_busy?, request_id?, upload_ids?)`：在原对话继续追问并沿用或覆盖选择，也可以附加新的流式上传文件。
- `chatgpt_resume(session_id, prompt, mode?, quality?, model?, reasoning_effort?, allow_downgrade?, on_busy?, request_id?, upload_ids?)`：按本地 `session_id` 恢复上次绑定的 ChatGPT 对话；会话过期后仍可重新打开原 URL 并继续上下文。
- `chatgpt_session_create(session_id?)`：创建或返回一个持久的逻辑会话 ID。
- `chatgpt_session_status(session_id)` / `chatgpt_sessions(include_dead?)`：查看会话 URL、LIVE/DEAD、过期时间和标签页是否打开。
- `chatgpt_session_close(session_id)`：显式标记会话 DEAD 并关闭该会话标签页。
- `chatgpt_cancel(task_id)`：取消任务。
- `chatgpt_takeover(task_id)`：返回登录、验证或澄清所需的人工操作。
- `chatgpt_browser_status()`：检查专用 Profile 是否已登录。
- `chatgpt_capabilities()`：读取当前账号实际可见的模型列表和能力滑块选项。
- `chatgpt_health()`：检查服务和任务存储状态。

任务状态会返回 `requested/resolved` 选择字段。若请求的 Pro 不在当前页面可用选项中，任务会进入 `MODEL_UNAVAILABLE`，不会静默降级；只有明确传入 `allow_downgrade=true` 才允许降到 Extra High。

共享浏览器一次只运行一个任务。占线时默认返回结构化 `BUSY`：`accepted=false`、`retryable=true`、`retry_after_seconds`、`active_task_id` 和 `queue_length`。客户端应延后重试；需要服务端排队时传 `on_busy="queue"`，拿到 `task_id` 后轮询该任务。使用 `request_id` 可以让客户端安全重试同一个提交而不重复创建任务。

当前 ChatGPT 页面实测的能力滑块为：`即时`、`中`、`高`、`极高`（Extra High）、`Pro`。页面菜单和标签会随账号、语言和产品更新变化，Broker 每次任务按可见控件重新发现，不保存内部模型 ID。

任务记录和结果位于 `runtime/jobs`。旧 `upload_id` 暂存在进程内存，默认 15 分钟过期且不能跨进程恢复。新版 file_ref 字节暂存于 `runtime/jobs/file-store`（或 `CHATGPT_FILE_STORE_DIR`），通过 SQLite 保存已提交 tus 偏移、SHA-256、状态和 TTL；未完成 source-download 在 Broker 重启后标记 `SOURCE_REFRESH_REQUIRED`，不会持久化签名 URL。单文件默认为 512 MiB、总暂存 2 GiB、每个 tus PATCH 最多 8 MiB、最多 4 个并行数据传输；这些是 Broker 上限，ChatGPT 上游仍有单独限制。轮询间隔默认是咨询/批评 30 秒、Deep Research 180 秒，测试时可以用环境变量缩短。
调度状态位于 `runtime/jobs/scheduler.sqlite3`，由 HTTP 和 stdio MCP 共享，保证同一 Windows 浏览器节点只有一个活动任务。浏览器等待队列最多容纳 8 个任务；超过时返回 `CHATGPT_QUEUE_FULL`。导出请求 envelope 与脱敏事件默认保留 90 天，可通过 `CHATGPT_EXPORT_AUDIT_RETENTION_SECONDS` 调整；过期的终态请求和事件会被定期清理，活动请求不会被清理。导出页请求数只作持久聚合计数，不设固定次数拒绝；READY 快照仍受 TTL 与 `CHATGPT_EXPORT_MAX_SNAPSHOT_BYTES` 容量限制，超容量会明确失败而非返回部分快照。

每个命名会话默认有 2 小时空闲保活（`CHATGPT_SESSION_TTL_SECONDS`）。submit/resume/export start 会刷新期限；
超时后会话持久记录变为 `DEAD`，仅关闭它自己的 ChatGPT 标签页，不删除 conversation URL。下一次
`chatgpt_resume` 会按该 URL 重新打开并恢复上下文。导出快照另有 15 分钟空闲 TTL 和 2 小时绝对期限；
session 自动过期不影响已经生成的快照，显式 `chatgpt_session_close` 会取消该 session 的导出并撤销快照。

导出不会自动随任务完成触发。`export_id` 固定某一刻的 session URL 和消息顺序；续聊或重导出会得到新 ID，
旧游标不会漂移到新内容。消息与附件完整性分别标记 verified/unverified/partial；缺少可信历史终点、附件来源不可用或任一文件传输校验失败时不会声称全量。
V2 的附件视图不包含源 URL，只有显式领取句柄后才可通过受 Bearer 保护的
`/exports/download/<handle>` 下载。ChatGPT 文件卡片没有静态链接时，服务端在登录浏览器内复用卡片动作，
不向客户端暴露签名 URL。受保护的 HTTP `POST /exports/<export_id>/page` 与 MCP 分页共用同一快照。
下载响应带 `Content-Length` 和 `X-Content-SHA256`；客户端应校验实际收到的字节后再确认文件传输完成。`scripts/export-e2e.py` 会逐项取回 manifest 中 available 或 candidate 的文件并校验摘要，不会落盘；unavailable 项或 candidate 下载失败会让附件验收失败。

导出视图保存在 HTTP Broker 私有的短期临时目录，分页索引和源 URL 仅在进程内；SessionStore 仍只保存 URL 和任务/TTL 元数据。
导出正文不进入 Scheduler SQLite、SessionStore 或日志。`runtime/jobs/scheduler.sqlite3`
持久保存不含正文的导出请求、状态和审计事件；`runtime/logs/chatgpt-oracle-[pid].log` 按进程轮转并过滤 URL、
Bearer 和认证值。Broker 重启后，QUEUED/RUNNING 请求可根据元数据重做；READY 快照因进程内正文丢失会变成
`EXPORT_GONE`，客户端应以新的 request_id 重导出。保留快照的磁盘总量受 `CHATGPT_EXPORT_MAX_SNAPSHOT_BYTES`
限制，默认 64 MiB，满时拒绝新快照并返回 `EXPORT_CAPACITY`，不会驱逐正在分页的快照。源 API 页现在逐页归一化到临时 JSONL spool，
不再整轮累积全部消息；采集时仍会在内存中持有一个完整 JSON 页及其归一化表示，单页峰值在响应解码后检查，不能视为零内存或任意大小支持。

V2 工具由唯一 HTTP Broker 持有快照和消费导出队列；stdio 客户端通过受保护的 loopback 路由转发。
远端需要归档文件时，可用 Bearer 保护的 `POST /exports/<export_id>/file`，请求体传 `session_id` 与
`view`，响应按文件流式返回。客户端必须等到所需视图 EOF，并核对 total_bytes/SHA-256 后，
才能报告这些已观察内容已完整交付。

## 安全边界

服务不会读取或导出 Cookie、localStorage、storage state、密码、OTP 或 OAuth Token，不自动处理 CAPTCHA 或第三方授权。遇到这些状态会返回 `NEEDS_USER_INPUT`，需要人工在专用浏览器窗口中完成后再继续。浏览器默认启动最小化窗口；可以在首次登录后设置 `CHATGPT_BROWSER_HEADLESS=1`，但是否适合当前账号需要用真实烟测确认。

## Codex Skill

项目内的 `skills/chatgpt-oracle/SKILL.md` 描述如何选择模式、提交任务、等待结果，以及按需调用分页导出工具；它只作为使用手册，不规定模型何时必须调用该 Skill。运行 `scripts/install-codex-skill.ps1` 可安装到当前用户的 Codex Skill 目录。
