# Session、并发与生命周期

在创建、续聊、排队或关闭会话时读取。先按 [compatibility.md](compatibility.md) 确认当前实例的并发和驻留能力。

本页为 ChatGPT 操作说明；Gemini 的独立 Session 与上游并发边界见 [gemini.md](gemini.md)。

## 建立与续聊

使用 `chatgpt_session_create` 创建命名 Session，或为首次 `chatgpt_submit` 传入稳定的 `session_id`。后续用 `chatgpt_resume(session_id, prompt, ...)` 在同一会话添加新一轮；不同 Agent 可以通过该名称共享上下文。

也可使用 `chatgpt_continue(task_id, prompt, ...)` 延续已完成或等待内容澄清的任务。它按工具参数继承或覆盖档位；需要改变模式时核对当前 `chatgpt_resume` 的 schema。每轮新问题与网络重试的区别见 [task-identity.md](task-identity.md)。

`chatgpt_session_create` 只建立本地映射，`tab_open=false` 与配置版本都不证明网页或 bootstrap 送达。支持 `oracle-v1` 的服务在新 Web 会话的第一条实际消息中合并首行 marker、Core、短 mode 提示与实际问题，只发送一个 turn；后续不重复 Core，每轮 mode 以当前 Job 为准，变化时可带短提示。提示文本本身不证明已启用网页 Deep Research。

仅建立已有 URL 的映射或同 URL 别名，以及状态查询与只读导出，不注入 Core 或新增问答。旧记录与仅有 URL 的 Session 缺少含 Core 的确认回执时保持 `unknown`，不自动补写历史；该会话的新 MCP 消息仍使用 marker。确认状态的依据见 [task-identity.md](task-identity.md)。

健康 LIVE Session 在一轮结束后保留独立标签页，同一 `session_id` 的后续对话复用原页。不要每答完一轮就关闭 Session；主 Skill 的多轮讨论在同一 Session 中继续。

人类可在 Broker 回合之间参与网页讨论；Broker FIFO 不能锁住人类编辑。支持草稿保护的实例发现 composer 非空时，以 `COMPOSER_DRAFT_PRESENT` / `user_input_kind="composer_draft"` 停止发送，不覆盖草稿；按 [runtime.md](runtime.md) 处理。不能据此承诺同时编辑没有竞态。

## 调度与资源

- ChatGPT 与 Gemini 共用最多四个驻留 Session，合计达到四个时，第五个新 Session 等待容量；已有 Session 的续聊仍可执行。各 Session 的问答、导出和附件浏览器操作按 FIFO 串行。
- 默认 `on_busy="queue"`；需要立即拒绝忙碌请求时传 `on_busy="reject"`。普通等待队列最多八项；`BUSY` 等回执按 `retry_after_seconds` 等待，不紧密重试。
- 指向同一 `conversation_url` 的不同命名 Session 可能共享原会话的串行操作队列。换一个名称不使同一网页会话独立执行，也不证明服务缺少四 Session 并发。
- 未命名兼容调用共用串行 legacy 队列，操作结束清理临时页；连续对话使用命名 Session。
- 两个服务共用一个专用 Profile、CDP 和最多四个物理标签页，人工接管、诊断、导出和下载均计入。复用对应 Session 页面或等待容量，不另开验证页绕过预算。
- 新页准备、加载、菜单、上传和发送依次执行；初始发送确认可短暂保留 UI 归属，避免另一提供方在提交交接中切换前台。后续生成与原页只读观察不持有 UI 锁，不同 Session 可同时生成。诊断与借用空闲 Session 的操作串行。

共享实例的 `scheduler.session_count` 是两者合计，`local_session_count` 和 `session_slots` 是当前提供方，`shared_session_slots` 展示全局带提供方前缀的槽位。任务、队列、导出与文件引用仍按提供方独立；相同公共 Session 名称不共享上下文或关闭权限。

## 结束与恢复

命名 Session 默认有两小时空闲期限。用 `chatgpt_session_status` 核对 LIVE/DEAD、URL 和 `tab_open`。显式关闭或过期回收该 Session 的页面与槽位，保留 URL；恢复 DEAD Session 需要重新加载原 URL，也可能等待容量。

支持该协议的实例恢复到原 URL 且有 Core 首轮确认依据时，只随新的明确续聊附短提醒“继续本对话已建立的 Oracle 协作约定。”，不重复 Core，也不单独发送确认轮。`unknown` 不声称已有约定；短提醒也不证明 Web 仍看到全部历史。查询和导出不触发提醒发送。

整个 Session 生命周期结束后，交付结果、释放导出和临时文件，再调用 `chatgpt_session_close(session_id)`，确认 `DEAD`、`tab_open=false`。所有工作结束后的单一未归属 `about:blank` 是浏览器保活页，不是仍驻留的对话。

错误或取消后还需续聊时，先按 [runtime.md](runtime.md) 处理状态，并核对 Session 是否仍 LIVE。命名 Session 的 ERROR 页面与未确认发送的自有页保留供核查，仍计入四页预算；过期、页面自身关闭或旧实例清理后可能不存在，因此仍需核对 `tab_open`。调查结束再显式关闭，不因 terminal=true 推断网页绝未发送或云端停止。人工接管期间同样保留页面。关闭返回 `SESSION_BUSY` 或 HTTP 超时时，不能推断已经释放资源。
