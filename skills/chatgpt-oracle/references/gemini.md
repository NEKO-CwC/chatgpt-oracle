# Gemini Web MCP

操作 Gemini 时读取。主 Skill 的咨询、批评、研究与多轮讨论方法共用；连接见 [connection.md](connection.md)，账号和资料边界见 [security.md](security.md)。工具以当前 `tools/list` 为准，不能从 ChatGPT 前缀替换推断全部能力。

## 工具与档位

Gemini 独立 MCP 提供 27 个 `gemini_*` 工具，与 ChatGPT 共用一个专用原生 Chrome Profile 和 CDP；服务端 Bearer、任务、Session、队列和文件存储仍独立。已启用 Tailscale 直连豁免时，远端客户端无需配置 Gemini Bearer；公网 / FRP 仍使用它。先用 `gemini_capabilities(scope="service")` 检查契约、`browser.pool_scope="shared_profile"` 与 `http_auth`；`scope="browser"` 检查账号当前可见选择器，可能操作诊断页面，不证明随后任务已就绪。

- `gemini_submit(mode="consult"|"critic"|"research", prompt=..., session_id=..., request_id=...)` 发起新任务；具体参数以 schema 为准。
- 默认选择可见 Argon / High；`quality` 接受 `auto/low/medium/high`，三个 mode 的 `auto` 均解析为 High。显式 `reasoning_effort` 覆盖 quality 档位别名。
- 可显式传 `model="Argon", reasoning_effort="high"`。ChatGPT 的 Extra High / Pro 不会隐式转译成 Gemini 档位；服务端先核查配置的已登录账号，全部不可用才返回 `MODEL_UNAVAILABLE`；只有用户允许才用 `allow_downgrade=true`。
- `selection_evidence.kind="visible_picker"` 证明可见菜单读回；`generation_model="unknown"` 表示缺少独立后台模型证据。不能把菜单文字当成服务端模型身份，也不能据此保证审美或识图质量。

## 多账号与额度

同一专用 Chrome 可以登录多个 Google 账号；`/u/0/app?pli=1`、`/u/1/app?pli=1` 是不同账号路由，正式 URL 保留 `/u/{index}/app/{conversation}`。旧 `/app/{conversation}` 兼容默认账号 0。`GEMINI_ACCOUNT_INDEXES` 配置可尝试的已登录索引，默认 `0,1`；可选 `GEMINI_PREFERRED_ACCOUNT_INDEX` 指定优先级。默认识别人工 URL 切换，并持久化最后验证可用账号；不自动登录。

明确指定账号时，先刷新 `tools/list`，确认包含 `gemini_account_select`，然后使用：

```text
gemini_account_select(account_index=0, strict=true)   # 固定 /u/0/app；账号 1 同理
gemini_account_select(account_index=0, strict=false)  # 优先 0，缺少模型或限额时允许回退
gemini_account_select(account_index=null)             # 清除显式选择，恢复自动路由
```

这是持久的服务级路由选择，对后续新会话生效，优先于自动偏好和配置的首选账号；同一服务的其他客户端也会看到该选择。已有 Session、草稿、在途任务和正式 URL 保留原账号；需要跨账号讨论时，先处理原任务的送达不确定性，再由 client compact／组装上下文并建立新的 Session 与 request。新建 Session 本身不会切换账号。

成功回执中的 `mode="explicit"`、`home_url`、`strict` 只证明路由配置已保存；`account_availability_verified=false` 明确表示没有证明该账号已登录、具备 Argon 或完成发送。用 service capabilities 的 `accounts.explicit_selection` 核对配置，用后续任务的 `account_index`、`account_attempts`、正式 URL 核对实际执行。严格固定 0 时若 0 不提供 Argon，服务会返回该账号的不可用证据，不再悄悄改用 1。允许回退时须按实际回执说明最终账号。

`gemini_capabilities(scope="browser")` 返回 `account_index` 和实际 URL，能力只属于该账号。新任务使用同一账号选择器；缺少模型先重读本账号菜单，再尝试其他配置账号。模型缺失可能来自套餐、发布范围、渲染或限额；只有明确网页额度提示才视为已观察限额。

新 Web 会话尚未上传、填写、发送时，服务端可切换账号后继续同一个 task，请求参数和输入保持，回执记录 `account_index`、`account_attempts`、`account_switch`。已经发送或已有对话时，服务端不重放问题、不假称跨账号继承记忆，也不把原会话 URL 直接改成另一账号。

**先于通用 ERROR 停止规则处理 `ACCOUNT_CONTEXT_REQUIRED`**：服务端已验证并选择 `account_switch.to_account_index` / `target_home_url`，该 task 本轮没有可用结果。client Main 保存原回执，然后自行 compact 或组装本轮所需的可核验上下文，在新 `session_id`、新 `request_id` 中提交新的讨论。不要调用旧 Session 的 resume 期待切换账号；新任务会使用服务已选择的账号。此项是用户要求的客户端恢复步骤，不需再让用户批准普通账号切换。

上下文包区分用户要求、本地事实、历史模型意见和未决问题，说明新账号没有旧会话记忆。需要继续识图或读附件时重新提供实际输入；旧 Session 的 `file_ref` 不能直接跨 Session 复用，应按文件哈希核对并重新上传。`send_attempted=true` 仍保留原送达不确定性，不把原问题当成确定未发送，也不让后台 watcher 自动复制原问题。保留原账号会话以供核查。

全部账号仍不可用时保留原错误和 `account_attempts`，不无限轮换、降档、切换提供方或重启研究。临时账号探测页同样占四页预算，探测完成即释放。

## Session、请求与取回

使用 `gemini_session_create`，或在首次 `gemini_submit` 指定稳定的 `session_id`。后续用 `gemini_resume(session_id, prompt, ...)` 或 `gemini_continue(task_id, prompt, ...)` 在原会话追问。每轮新问题用新的 `request_id`；网络重试同一请求复用原 ID 和参数。保存 `session_id`、`request_id`、`task_id`、原 URL 和档位回执，不因超时、等待或断线重提原问题。

服务在新 Web 对话首轮合并一次 Core bootstrap，每条 MCP 新消息以 `【CODEX_ORACLE】` 为首行；客户端不复制整份 Core。`prepared` 不等于已发送，Session 配置也不等于 bootstrap 已确认；以任务的发送状态、匹配用户/助手 turn 和确认回执判断。网页 `user` 角色或 marker 不能单独认证人类或推导授权。

ChatGPT 与 Gemini 合计最多四个驻留 Session / 物理标签页，人工页与诊断页计入；每个 Session 内问答和文本快照按 FIFO 串行。默认 `on_busy="queue"`，按返回提示等待；另一提供方的驻留 Session 也占共享容量。上游账号允许同时生成/研究的数量未由本地四槽位证明，不承诺四路上游并发。LIVE Session 在两轮之间保留页面；整个讨论结束并释放快照和输入文件后，才用 `gemini_session_close` 关闭并核对资源状态。`gemini_session_status` / `gemini_sessions` 用于查看持久映射；ChatGPT 的同名 Session 不共享该上下文。

提交后由一次启动的持续后台脚本查询 `gemini_status(task_id)` 并等待，避免每个间隔重新消耗主模型轮次，见 [background-wait.md](background-wait.md)。仅在 `COMPLETE` 时取 `gemini_result`。大结果用 `delivery="reference"` 获取 `result_id`，再用 `gemini_result_page` 按游标到 EOF，重组并核对长度与 SHA-256；客户端截断的 inline 内容不能当成完整结果。结果页游标不能与会话快照游标混用。

## Deep Research

新研究通过新 Session 的 `gemini_submit(mode="research")` 发起；原研究 Session 的续聊用于讨论已有报告。区分研究计划、启动动作和报告正文，不能把计划或“研究中”卡片当成完成报告。

点击 Start research 会由网页插入额外用户 turn。服务将该动作与报告单独关联，保留原始问题身份；不能把额外 turn 当成原问题重发或新的人工授权。研究启动意图在点击之前持久化；出现 `RESEARCH_START_UNCONFIRMED` 时保留原任务、只读核查，不再点击或新开任务重试。

`UPSTREAM_LIMIT` 表示已观察账号额度限制。服务端可按上述多账号策略选择替代账号；返回 `ACCOUNT_CONTEXT_REQUIRED` 时由 client 重建上下文，不能再次点击旧研究计划或重放历史研究启动。完整真实研究报告的验收边界仍按实际结果记录，切换账号或出现计划不证明研究报告已经完成。

## 文件输入与边界

普通文件（含 TXT）和图片可作为输入；单轮最多 10 个、单文件 100 MiB，网页还可能有更严格限制。ZIP 在提交、续聊和 file_ref 重用入口均拒绝；不绕过限制或要求上游打包。

文件传输和文本快照需要 HTTP Broker；stdio 使用同一 Gemini Bearer 转发到本机 Broker，无文件问答可单独使用 stdio。不要把客户端路径或大段 base64 当作远程文件引用。

1. 在目标 Session 中调用 `gemini_file_upload_start`，按回执的 tus endpoint/metadata 创建并上传字节，保存 `transfer_id`。
2. 用 `gemini_transfer_status` 核对完成偏移、长度、哈希和 `file_ref`；用 `files=[{"file_ref":"..."}]` 提交给同一 Session 的 submit/resume/continue，以当前 schema 为准。旧 `POST /uploads` + `upload_ids` 是短期内存兼容入口，不能与 file_ref 混用。
3. 分别确认 Broker 收到字节、网页附件挂载，以及回答对文件内部具体内容或图像区域的识别。暂存成功不保证上游接受，网页 verified 不单独证明模型读到内容。
4. 用 `gemini_file_info` 查询暂存输入元数据；完成后 `gemini_file_release`。未完成传输使用 `gemini_transfer_cancel`。

本 MCP 不提供 `gemini_export_attachment_handle` 或 `gemini_file_download_start`；不兼容通用生成附件、ZIP 打包及源附件下载。`file_info` 的下载路径仅用于 Broker 自己暂存的输入字节，不能据此宣称从 Gemini 导出了原附件。用户明确要求生图可在上游网页对话中提出，本期 MCP 交付文本与原会话链接，不承诺生成图片的二进制下载。

## 已观察文本快照

调用 `gemini_export_start`，保存 `session_id` / `export_id`，用 `gemini_export_status` 等到 `READY`；等待也交给上述后台脚本。按需用 `gemini_export_page` 读取 `messages` / `markdown` 到 EOF，并重组核对长度和 SHA-256；`attachments` 为空视图，输入原件不属于快照范围。旧 inline `gemini_export` 不适合长结果。

快照仅覆盖网页已观察文本，`completeness.messages="unverified"` 保留历史覆盖缺口。即使请求 `scope="all"` 或分页哈希通过，也不证明取得任意长会话的可信历史终点；不能把空附件视图解释为原对话没有附件。READY 只表示快照可读，EOF/hash 只证明该快照传输完整。

完成后 `gemini_export_release`；排队/运行中的快照用 `gemini_export_cancel`，不是 release。快照过期需要新的导出请求，不重发问答。

## 澄清、取消与故障

`NEEDS_USER_INPUT` 时用 `gemini_takeover` 核对本轮实际阻塞。Main 能根据本地事实和现有授权回答的内容澄清，在同一 Session 用新 request_id 续聊；登录、验证码或账号保护交给人工。`COMPOSER_DRAFT_PRESENT` 保留草稿，不自动清空或覆盖。

`gemini_cancel(task_id)` 后核对终态、`cancellation_stop_verified` 和资源回执。`CANCEL_REQUESTED` 只是意图；`CANCEL_STOP_UNCONFIRMED` 或 stop=false 不证明上游已经停止，本地页面回收也不能替代停止证据。保留原身份和 URL，只读核查。

`TIMEOUT`、`SEND_UNCONFIRMED`、`TURN_IDENTITY_UNAVAILABLE` 和浏览器错误都不能证明问题未发送。先查原任务和已有 URL，不以新 request_id 重提。`gemini_health` / `gemini_browser_status` 用于服务/浏览器诊断，不能替代本轮发送或内容证据。

首次发送缺少正式 URL 和本轮身份时，服务独立执行有界确认，默认约 60 秒加一次有界观察后返回 `SEND_UNCONFIRMED`，保留原页与回执。Session 未绑定时不能用 URL 导出；不要因 `SESSION_UNBOUND` 就另发原问题。此确认预算不限制已确认回答的正常生成时长。

`BROWSER_UI_BUSY` 且任务仍为 `QUEUED` 时，表示初始交互锁尚未获准；用后台脚本等待同一任务，不把它当成需要重新 submit 的失败。`BROWSER_UI_WAIT_TIMEOUT` 表示准入等待预算已耗尽，交回 Main 处理。普通 `BROWSER_LOCK_TIMEOUT` 可能发生在其他阶段，不能据此重放任务。
