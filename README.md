# chatgpt-oracle

本项目提供 ChatGPT Web 与 **Gemini Web MCP**。两个提供方共用一个原生 Chrome Profile、CDP 和合计四个驻留 Session / 标签页；各自的 MCP、任务、会话上下文、队列与文件存储仍独立。现有工具名称和调用方式保留。

## 共享浏览器

默认启用 `ORACLE_SHARED_BROWSER=1`，两个服务都连接本机 `http://127.0.0.1:9333`。本机已复用登录了两个站点的 `~/.agent-browser/gemini-native-profile`；旧 ChatGPT CDP 9222 已停用，旧 Profile 文件保留。新项目未指定 Profile 时使用 `runtime/oracle-browser-profile`。

共享配置使用 `ORACLE_BROWSER_PROFILE_DIR`、`ORACLE_BROWSER_CDP_URL` 和 `ORACLE_BROWSER_LAUNCH_CDP`。Windows 启动器及配置解析会读取这几个非秘密的用户级设置，已打开的旧终端也可取得共享配置；显式进程环境变量优先。共享模式下忽略旧的提供方独立 Profile/CDP 选项，`ORACLE_SHARED_BROWSER=0` 仅保留兼容运行方式。

需要初始化或人工登录时运行：

```powershell
.\scripts\setup-browser.ps1 -Interactive
```

两个 MCP 服务分别启动或由各自计划任务运行。比如 2 个 ChatGPT + 2 个 Gemini 已占满共享容量；第 5 个新 Session 排队，已有 Session 可继续，释放一个后队列推进。人工页、诊断与辅助页也计入四个物理页。Session 名称按提供方区分，关闭一边的同名 Session 不关闭另一边；服务断开时保留共享 Chrome。

## Gemini Web MCP

先在共享原生 Chrome 中完成所需账号登录，再启动 Gemini 服务：

```powershell
.\scripts\setup-browser.ps1 -Interactive
.\scripts\start-gemini-mcp.ps1 -Transport http
```

本机入口为 `http://127.0.0.1:8788/mcp`，健康检查为 `/healthz`。也可运行 `python -m chatgpt_oracle.gemini_server --transport http`，或不带 `--transport` 使用 stdio。安装 Python 包后可用 `gemini-oracle` 命令。浏览器 Profile 与 ChatGPT 共用，CDP 仅监听本机 9333；服务通过同一原生 Chrome 连接/启动路径工作，不自动填写登录、验证码或读取认证材料。`setup-gemini-browser.ps1` 在共享模式下也委托共同初始化脚本。

文件传输与文本快照沿用唯一 HTTP Broker 架构。使用这些能力时需要运行上述 HTTP 服务；stdio 端通过同一 `GEMINI_ORACLE_BEARER_TOKEN` 转发到本机 Broker。无文件的问答可单独使用 stdio。

Gemini 暴露 26 个工具，使用 `gemini_` 前缀。支持 submit/status/result/result_page、continue/resume、session_create/status/close/sessions、cancel/takeover、browser_status/capabilities/health、file_upload_start/transfer_status/transfer_cancel/file_info/file_release，以及 export/start/status/page/cancel/release。语义对应下文 ChatGPT 工具，主要差异如下：

- 模型默认选择账号可见的 Argon，思考档位默认 High。`quality` 接受 `auto/low/medium/high`，也可显式传 `model="Argon"` 和 `reasoning_effort="high"`；显式 reasoning_effort 覆盖 quality 档位别名。ChatGPT 的 Extra High / Pro 别名不会隐式转译到 Gemini。
- 模型不可用时返回 MODEL_UNAVAILABLE；只有显式 `allow_downgrade=true` 才允许选择其他可用模型。`selection_evidence.kind="visible_picker"` 表示实际选择器读回；`generation_model="unknown"` 明确表示没有独立的后台模型回执。可见模型或档位发生变化时任务失败，不自动重提。
- TXT、图片及网页支持的普通文件可输入。本期 Broker 每轮上限为 10 个文件、单个 100 MiB；网页仍按其自身限制验证，暂存成功不保证上游接受。ZIP 在新提交、续聊及 file_ref 重用入口均拒绝。传输到 Broker、网页附件挂载及模型实际读取分别举证。
- 不提供 `gemini_file_download_start` 或 `gemini_export_attachment_handle`，不兼容通用生成附件、ZIP 打包或源附件下载。file_info 的下载路径仅用于 Broker 自己暂存的输入字节。显式生图可在上游网页对话中提出，本期 MCP 交付文本与原对话链接，不承诺图片二进制交付。
- 导出契约是**已观察文本快照**，包含消息、Markdown 和空附件视图；不宣称取得任意长历史的可信终点。`completeness.messages="unverified"` 保留覆盖范围的不确定性；分页 EOF 与 SHA-256 证明快照传输，不证明原会话历史完整。输入附件原件不纳入该快照范围。
- research 使用 Deep Research，区分计划、启动和报告。启动意图在点击之前持久化，启动不确定时不重复操作。新研究通过新 Session 的 `gemini_submit(mode="research")` 发起；原 Session 的续聊用于讨论已有报告。
- 点击 Start research 会由网页插入一条新的用户消息；服务单独关联该提供方动作及后续报告，保留原始问题回执。额度横幅返回 `UPSTREAM_LIMIT`，不重复启动或改用其他模型。完整报告需要读取报告面板正文，不能用“研究中”卡片或研究计划冒充结果。
- 两个提供方合计最多四个驻留 Session / 物理标签页，人工页也计入；会话内 FIFO 串行。Gemini 上游同时生成、尤其同时研究的上限另由账号决定，共享的四个本地槽位不代表四个上游任务已经验收。

```json
{
  "mode": "consult",
  "prompt": "请根据这份材料提出不同解释。",
  "session_id": "design-discussion",
  "model": "Argon",
  "reasoning_effort": "high",
  "request_id": "one-request-identity"
}
```

把以上参数交给 `gemini_submit`；用返回的 task_id 查询 `gemini_status`，完成后读取结果。之后使用同一 session_id 调用 `gemini_resume`，每轮新问题采用新的 request_id；网络重试保留原 request_id 和参数，不重复发送。仅在整个讨论结束后关闭 Session。

Gemini 配置使用 `GEMINI_*` 环境变量，不继承 ChatGPT 的 Bearer、目录或端口；默认数据位于 `runtime/gemini/jobs` 和 `runtime/gemini/sessions`。除了 HTTPS 域名，也可通过受限 Tailscale 网络使用 `http://100.77.245.1:8788/mcp`。配置 `GEMINI_ORACLE_TAILSCALE_IPS=100.77.245.1` 并启用远程监听后，Tailscale 直连无需 Bearer；公网 / FRP 与 loopback 内部 Broker 继续使用独立的 `GEMINI_ORACLE_BEARER_TOKEN`。精确 IP Host 自动加入 allowlist，额外域名另配 `GEMINI_ORACLE_ALLOWED_HOSTS`。配置条件和示例见 [Skill 连接说明](skills/chatgpt-oracle/references/connection.md)。

Gemini 多账号使用 `/u/0/app?pli=1`、`/u/1/app?pli=1`，`GEMINI_ACCOUNT_INDEXES=0,1` 配置已经登录的账号索引；可选 `GEMINI_PREFERRED_ACCOUNT_INDEX=1` 固定优先级。默认检测人工 URL 切换并记住最后可用账号。新会话在填写／上传前模型不可用或明确限额时，可在同 task 内换账号，保留请求和附件；已有对话始终绑定原账号。需要换账号续聊时，服务选定可用账号并返回 `ACCOUNT_CONTEXT_REQUIRED`、`account_switch`，由 client compact 或组装上下文后以新 Session、新 request 提交，不伪称继承另一账号记忆。能力检测返回 `account_index`，正式 URL 保留账号前缀。

本地的 ChatGPT Web 外部研究桥接器。它把 ChatGPT Web 当作一个异步的咨询、研究和批评角色，向 Codex 暴露语义级 MCP 工具。浏览器 worker 只连接本机 `127.0.0.1` 的正式 Chrome CDP，不把 CDP 暴露给 MCP 或 FRP，不读取 Cookie。

四 Session 并发、原页续聊与故障收束已部署并通过真实验收，证据见 [CR-20261003-002](docs/changes/CR-20261003-002.md)。远程客户端可用 `chatgpt_capabilities(scope="service")` 核对所连服务的 `browser.max_sessions`、`operations_per_session` 和 `tabs_retained_until`；更新 Skill 本身不会升级服务。

Oracle 发送协议见 [CR-20261005-002](docs/changes/CR-20261005-002.md)：每轮 MCP 消息自动以 `【CODEX_ORACLE】` 开头，普通 New Chat 的首问合并一次版本化 bootstrap，续聊保留原页和原始请求身份。能力/回执区分配置、prepared、confirmed 与 unknown；Memory 仅有提示约定。完整 Core 属服务安装资源，客户端 Skill 为主文件与十份 references，含 Gemini 独立操作说明。

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

窗口中完成 ChatGPT 登录后按 Enter。共享 Profile 默认位于 `runtime/oracle-browser-profile`，本机通过用户级设置复用已登录的专用原生 Profile；不会自动读取日常 Chrome Profile。`runtime/chatgpt-browser-profile` 仅为关闭共享模式时的旧默认值。

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

默认本机 Streamable HTTP MCP 地址为 `http://127.0.0.1:8787/mcp`，健康检查为 `/healthz`；当前实际部署见下一段。已配置过的公网 MCP 地址为 `https://chatgpt-oracle.frp-service.neko-dashboard.com:32751/mcp`。通过 FRP 暴露时，将 `CHATGPT_ORACLE_ALLOWED_HOSTS` 设置为 FRP 请求使用的精确 Host，并把外部入口转发到该 MCP 端口；不要转发 Chrome CDP 端口。

2026-10-14 的 Tailscale 部署使用 ChatGPT `http://100.77.245.1:8787/mcp` 与 Gemini `http://100.77.245.1:8788/mcp`，两者均已从另一台 tailnet 主机无 Bearer 验证 health、MCP initialize 和工具发现。ChatGPT 当前只监听 Tailscale IP，因为 loopback 8787 被 Kiro 占用；Gemini 监听 `0.0.0.0:8788`。防火墙限定 Tailscale 网卡、节点 IP 与 tailnet 来源；两个隐藏的 Windows 登录启动任务保持服务运行。公网 FRP 的 TLS 故障未在本次修复。

2026-10-03 的 ZIP 验收通过受限 Tailscale 网络访问 `http://100.77.245.1:8787/mcp`，沿用 `Host: chatgpt-oracle.frp-service.neko-dashboard.com:32751`、Bearer 认证，并绕过环境代理。公网 FRP 当时的 TLS/连接失败与直接使用 IP Host 得到的 421 属于连接配置问题，与网页选择器错误分别排查；该次验收不代表公网 FRP 已恢复。

如需注册 Windows 登录后自动启动的 HTTP Broker：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\register-http-mcp-task.ps1 -AllowedHosts 'chatgpt-oracle.frp-service.neko-dashboard.com:32751'
```

需要监听所有 IPv4 网卡时，启动脚本或注册脚本可加 `-ListenHost '0.0.0.0'`；注册后计划任务会保留该监听地址。公网 / loopback 的 Bearer 认证及 MCP Host allowlist 仍然生效，Chrome CDP 继续只监听本机。

注册 Gemini 或启用 Tailscale 免 Bearer 入口时，注册脚本支持提供方和精确 IP 参数。例如：

```powershell
.\scripts\register-http-mcp-task.ps1 -Provider gemini -ListenHost '0.0.0.0' -TailscaleIps '100.77.245.1' -PublicBaseUrl 'http://100.77.245.1:8788'
```

`-TailscaleIps` 会自动加入 IP Host allowlist；同样适用于 ChatGPT。`-PublicBaseUrl` 使文件传输回执使用 tailnet 客户端可达的地址。注册任务本身不修改防火墙，仍需按实际网络范围配置放行。

Gemini 显式选账号使用 `gemini_account_select(account_index=0或1, strict=true)`，再建立新 Session。该服务级持久选择优先于自动偏好，严格模式不自动换到其他账号；`strict=false` 允许回退，`account_index=null` 恢复自动路由。已有会话和在途任务继续使用原账号；账号选择成功不证明上游登录、模型可用或发送确认，须核对任务回执。客户端同步新工具列表及 [Gemini 说明](skills/chatgpt-oracle/references/gemini.md)。

HTTP 启动器在绑定具体网卡 IP 前，最多等待该地址就绪 300 秒，再检查端口并初始化 Broker，避免登录任务早于 Tailscale 网卡启动而直接退出。可用 `-StartupWaitSeconds` 调整等待上限；loopback、通配监听与域名不等待本机 IP 分配。地址等待记录位于 `runtime/logs/chatgpt-startup.log` / `gemini-startup.log`，底层 HTTP 启动和绑定错误写入对应 `*-oracle-<PID>.log`。新注册任务失败后每分钟重试，最多 10 次，允许电池供电时持续运行。2026-10-18 已将现有 ChatGPT 登录任务同步为该设置并重载；Gemini 运行进程保持，未重新注册其任务。

注册脚本会在当前 Windows 用户环境中生成并保存公网和本机内部转发使用的 Bearer Token。公网客户端使用 `Authorization: Bearer <token>`，从安全凭据存储或受支持的环境变量引用读取，不要提交到仓库或日志。启用 `*_ORACLE_TAILSCALE_IPS` 的 Tailscale 直连不需要 Bearer，也不需要在远端保存 Gemini 凭据文件；服务仍按实际 socket 两端地址判断，不能用代理头或 Host 冒充 Tailscale 连接。

```powershell
$env:CHATGPT_ORACLE_BEARER_TOKEN = [Environment]::GetEnvironmentVariable('CHATGPT_ORACLE_BEARER_TOKEN', 'User')
```

## MCP 工具

- `chatgpt_submit(mode, prompt, project?, model?, quality?, reasoning_effort?, allow_downgrade?, on_busy?, request_id?, upload_ids?, files?, session_id?)`：创建任务并选择模型/能力；传入新的 `session_id` 后，该 Session 获准执行时绑定独立 ChatGPT 标签页。`upload_ids` 是旧内存上传入口；`files=[{"file_ref": "..."}]` 可把已验证文件交给该 Session 的浏览器上传。
- `chatgpt_status(task_id)`：返回任务状态，不阻塞等待。
- `chatgpt_result(task_id, delivery="legacy"|"inline"|"reference")`：默认保留原内联响应；`reference` 返回稳定的 `result_id`，可用于逐页读取。显式 `inline` 超过配置响应大小时返回 `RESULT_TOO_LARGE`，并给出分页入口。
- `chatgpt_result_page(session_id, result_id, cursor?, max_response_bytes=8192)`：读取带 SHA-256 和 EOF 标记的 UTF-8 结果页。文件结果游标和会话导出游标互不通用。
- `chatgpt_export_start(session_id?, task_id?, conversation_url?, scope="all"|"recent", count=10, request_id?, on_busy?)`：开始一次 session 绑定的不可变导出快照，立即返回 `session_id`、`export_id` 和排队状态。相同 request_id 在同一 session 下幂等；输入不同返回 `IDEMPOTENCY_CONFLICT`。`count` 只约束 `recent` 的 user 回合数，不限制 `all` 的源 API 页数。
- `chatgpt_export_status(session_id, export_id)`：返回 QUEUED/RUNNING/CANCEL_REQUESTED/CANCELLED/READY/FAILED/NEEDS_USER_INPUT/GONE/RELEASED、消息与附件分项完整性、已处理分页请求计数、各视图字节数和 SHA-256。READY 只表示快照可读，不证明历史完整。
- `chatgpt_export_page(session_id, export_id, view, cursor?, max_response_bytes=8192)`：读取 `messages`、`attachments` 或 `markdown` 的有界 UTF-8 分页。响应上限 16 KiB；相同游标可安全重试；不因页数达到固定次数而提前拒绝，末页返回 `eof=true` 和空 `next_cursor`。
- `chatgpt_export_cancel(session_id, export_id)`：取消排队或运行中的导出；运行中会中断当前浏览器读取并等待槽位释放。若暂未释放，会返回 `CANCEL_REQUESTED`，可继续查状态。
- `chatgpt_export_attachment_handle(session_id, export_id, attachment_id)`：为 manifest 中标记 `available` 或 `download_candidate` 的附件领取短期句柄。candidate 表示需要通过该 Session 的浏览器操作尝试 ChatGPT 文件卡片动作，不代表已确认可下载。用 Bearer 认证 GET 返回的 `download_url`；响应包含 `Content-Length` 与 `X-Content-SHA256`，客户端应核对后才确认传输完成。失败后可重新领取句柄并报告 unavailable。
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
- `chatgpt_takeover(task_id)`：返回阻塞原因和接管说明。登录、验证或授权交给人工；普通内容澄清可由 Main 根据本地事实与已有授权回答，再在同一 Session 续聊。
- `chatgpt_browser_status()`：检查专用 Profile 是否已登录。
- `chatgpt_capabilities()`：读取当前账号实际可见的模型列表和能力滑块选项。
- `chatgpt_health()`：检查服务和任务存储状态。

任务状态会返回 `requested/resolved` 选择字段。若请求的档位确实不可用，任务会返回 `MODEL_UNAVAILABLE`，不会静默降级；只有明确传入 `allow_downgrade=true` 才允许 Pro 降到 Extra High。`quality="extra_high", allow_downgrade=false` 必须实际选择并验证 Extra High。仅指定 quality 时，`resolved_model=null` 不单独表示失败，应核对解析档位和选择验证结果。

模型选择前会在配置的总预算内等待输入框与可用选择器；只允许对受管新页在附件上传、发送之前重载一次。预算耗尽返回 `BROWSER_NOT_READY`，登录或验证交给人工；附件上传后会再次读回档位，变化时拒绝发送。服务接收上传字节、网页附件验证、模型读到内容是三项不同证据；需要证明读取 ZIP 时，应核对回答对 ZIP 内具体内容的引用。选择器与 ZIP 链路证据见 [CR-20261003-001](docs/changes/CR-20261003-001.md)。

浏览器调用与锁等待有配置控制的截止时间；`BROWSER_UNRESPONSIVE` 不能证明问题未发送，不自动重提原问题。故障驱动卡在取消时会被本实例结束，按归属回收页面；Chrome 最后留一个无归属空白页。新页准备/加载和输入依次执行，回答生成仍可四 Session 并发。

发送回执分别记录 `submission_state`、`current_user_message_id`、`current_assistant_message_id` 和确认时间；选择档位或进入 RUNNING 不单独证明发送完成。续聊按本轮提示词与消息身份关联回答，不以可见节点数量增加推断新回答。取消会唤醒长轮询并中断正在等待的页面读取，尝试可见停止控件，独立核对生成状态。`CANCEL_STOP_UNCONFIRMED` 表示本地任务与自身页面回收，尚未证明 ChatGPT 云端停止；保留原请求身份和 URL，不自动重提。没有页面/槽位的只读导出 Session 关闭仅处理自身元数据；同一 URL 的串行操作不会因另起 Session 名而变成独立执行。

两个 MCP 服务共用一个专用 Profile，合计最多四个物理标签页；首页、人工接管页、导出和下载辅助页都计入上限。合计最多四个驻留 Session，各 Session 内的问答、导出和附件浏览器操作按 FIFO 串行。已驻留 Session 在两轮之间保留标签页和槽位；第五个新 Session 等待释放容量，但不会阻挡已有 Session 续聊。无 Session 的兼容任务各用本提供方的串行 legacy 队列，操作结束释放临时页面和槽位；这些操作也受共享浏览器预算约束。

浏览器诊断借用一个空闲 Session 的页面并进入其队列；导出和下载复用对应 Session 页面或等待容量。切换模型、上传和发送使用短暂的 UI 互斥，模型生成与只读轮询不持有该互斥，因此不同 Session 可同时生成回答。默认 `on_busy="queue"`，拿到 `task_id` 后查询任务；显式传 `on_busy="reject"` 可立即返回结构化 `BUSY`。普通等待队列最多八项；关闭和过期清理可释放满额页面，避免等待中的新 Session 阻挡清理。

一次问答完成后只释放该 Session 的操作占用，LIVE Session 保留原标签页；后续以相同 `session_id` 调用 `chatgpt_resume`，直接在原页续聊。每轮新问题使用新的 `request_id`；网络重试同一请求则复用原 `request_id` 与原参数，避免重复发送。任务失败或取消后先检查状态并处理相应错误，不自动重发问题。仅在整个 Session 生命周期结束、交付结果并释放导出和临时文件后调用 `chatgpt_session_close`，回收该 Session 的页和槽位；不要每答完一轮就关闭。人工接管页面也计入四页预算。服务会记录页面归属，在后续分配时回收已退出进程遗留的页面；CDP 连接失败不会另启自动化 Profile。早期资源预算验收见 [CR-20261002-002](docs/changes/CR-20261002-002.md)，其全局串行与每轮关闭页面的旧策略由本次契约替代。

2026-10-02 实测的能力滑块为：`即时 / Instant`、`中 / Medium`、`高 / High`、`极高 / Extra High`、`Pro`。页面菜单和标签会随账号、语言和产品更新变化，Broker 每次任务按可见控件重新发现，不保存内部模型 ID。当前版本支持新版模型菜单、临时会话地址切换及 Deep Research 应用标记和嵌套报告组件；验收与已知边界见 [CR-20261002-001](docs/changes/CR-20261002-001.md)。

任务记录和结果位于 `runtime/jobs`。旧 `upload_id` 暂存在进程内存，默认 15 分钟过期且不能跨进程恢复。新版 file_ref 字节暂存于 `runtime/jobs/file-store`（或 `CHATGPT_FILE_STORE_DIR`），通过 SQLite 保存已提交 tus 偏移、SHA-256、状态和 TTL；未完成 source-download 在 Broker 重启后标记 `SOURCE_REFRESH_REQUIRED`，不会持久化签名 URL。单文件默认为 512 MiB、总暂存 2 GiB、每个 tus PATCH 最多 8 MiB、最多 4 个并行数据传输；这些是 Broker 上限，ChatGPT 上游仍有单独限制。轮询间隔默认是咨询/批评 30 秒、Deep Research 180 秒，测试时可以用环境变量缩短。
ChatGPT 的队列与导出元数据位于 `runtime/jobs/scheduler.sqlite3`，Gemini 位于 `runtime/gemini/jobs/scheduler.sqlite3`，各自的 HTTP/stdio 共享。共同驻留槽位保存在 Profile 旁的 `<profile-name>.oracle-pool.sqlite3`，合计最多四个、每个 Session 最多一个活动操作；提供方前缀隔离同名 Session，独立队列的 worker 不领取对方任务。各提供方等待队列最多 8 项。健康回执的 `session_count` 是全局合计，`local_session_count` 是本提供方，`shared_session_slots` 为全局槽位。导出 envelope 与脱敏事件默认保留 90 天，过期终态会定期清理；活动请求不会清理。导出页请求数只作计数，READY 快照仍受 TTL 与容量限制，超容量明确失败。

每个命名会话默认有 2 小时空闲期限（`CHATGPT_SESSION_TTL_SECONDS`），获准执行后在 LIVE 期间保留自己的标签页。submit/resume/export start 会刷新期限；
超时后会话持久记录变为 `DEAD`，仅关闭它自己的 ChatGPT 标签页并释放驻留槽，不删除 conversation URL。下一次
`chatgpt_resume` 会按该 URL 重新打开并恢复上下文。导出快照另有 15 分钟空闲 TTL 和 2 小时绝对期限；
session 自动过期不影响已经生成的快照，显式 `chatgpt_session_close` 会取消该 session 的导出并撤销快照。

导出不会自动随任务完成触发。`export_id` 固定某一刻的 session URL 和消息顺序；续聊或重导出会得到新 ID，
旧游标不会漂移到新内容。消息与附件完整性分别标记 verified/unverified/partial；缺少可信历史终点、附件来源不可用或任一文件传输校验失败时不会声称全量。

正文视图到 EOF 并匹配哈希，只证明对应视图取回；附件清单只列元数据和消息关联，不携带所有文件二进制，也不把图片自动作为接收模型的输入。需要判断图像或文件内容时，按任务需要下载并实际打开图片、读取文件或解压 ZIP，分别报告传输与内容识别证据。Library 原件引用与 image_asset_pointer 可以对应同一用户文件；图片表示可下载不证明原始上传字节已恢复，Library 来源缺口也不能写成原件失效。当前 Library 原件下载仍有缺口。

正常 `chatgpt_export_release` 返回 `state=RELEASED, accepted=false, error_code=null`；accepted 表示活动/可接受状态，不是所有工具通用的成功标志。客户端按对应工具的终态和错误码判断。`mcp==1.30.0` 是 SDK 依赖版本，服务实现与修复是否部署需核对实际回执、能力和实例验收。
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

项目内的 [skills/chatgpt-oracle/SKILL.md](skills/chatgpt-oracle/SKILL.md) 同时说明 ChatGPT Web 与 Gemini Web 的跨 harness 价值、调用边界与多轮讨论方法，保留 `$chatgpt-oracle` 名称。普通独立调查、代码审查和反例搜索优先使用 Codex Team；当 Web harness 或不同模型可能改变问题定义、视觉理解、判断或研究时才考虑 Oracle。共同连接与资料边界、ChatGPT 操作，以及 [Gemini 档位、Session、研究和文本快照](skills/chatgpt-oracle/references/gemini.md) 放在 [references/](skills/chatgpt-oracle/references/)，仅在相应操作需要时读取。运行 `scripts/install-codex-skill.ps1` 会安装完整 Skill 目录。

远程客户端同步最新 [delivery/chatgpt-oracle/](delivery/chatgpt-oracle/) 整个目录（`SKILL.md` 与十份 references），不能只复制主文件；历史日期的 ZIP 不含此次 Gemini 更新。旧 [delivery/SKILL.md](delivery/SKILL.md) 保留为指向当前目录的兼容入口。MCP 连接配置独立于 Skill：支持域名或 Tailscale IP + 端口，ChatGPT 地址为 `http://100.77.245.1:8787/mcp`，Gemini 地址为 `http://100.77.245.1:8788/mcp`。启用 Tailscale 直连后这两个入口无需 Bearer；公网入口仍需鉴权。监听、防火墙、精确 Host allowlist 和代理绕过条件见 [连接说明](skills/chatgpt-oracle/references/connection.md)；`0.0.0.0` 只作监听地址。客户端重新加载 Skill 并刷新 MCP 工具列表后，以对应 `chatgpt_capabilities(scope="service")` / `gemini_capabilities(scope="service")` 核对服务契约，再使用该提供方原 `session_id` 续聊。
