# 档位、等待、取消与故障

在选择模型能力、等待任务、取回结果或处理异常时读取。发送关联见 [task-identity.md](task-identity.md)，资源生命周期见 [sessions.md](sessions.md)。

本页为 ChatGPT 档位和状态说明；Gemini 的 High 档位、额度限制与取消处理见 [gemini.md](gemini.md)，不套用 Extra High / Pro。

共享浏览器实例的四个驻留槽位由两个提供方合计使用。`SESSION_CAPACITY` 可能来自另一提供方占用；按回执排队或关闭已结束的 Session，不因此切换提供方、降低档位或重发问题。

## 实际档位

当前服务中，`quality="auto"` 对 `consult` / `critic` 默认解析为 `extra_high`，对 `research` 默认解析为 `pro`。用户明确要求 Pro 时传 `quality="pro"`；更强的普通推理使用 `quality="extra_high"`。需要具体 `model` 或 `reasoning_effort` 时核对当前账号能力与工具 schema，不猜测标签。

调用前可用 `chatgpt_capabilities` 查看账号可见模型与能力滑块。结果核对 `resolved_reasoning_effort` 和选择验证；仅指定 quality 时，`resolved_model=null` 不单独表示失败。附件上传后服务会再次验证档位。

`allow_downgrade=false` 必须保持请求语义。Extra High 或 Pro 确实不可用时返回 `MODEL_UNAVAILABLE`；只有用户明确允许才传 `allow_downgrade=true`，不静默改用其他档位。

## 等待与取回

保存提交回执后使用 `chatgpt_status(task_id)` 查询，建议把状态查询、等待与结果落盘放进一次启动的持续后台脚本；不要让主模型每隔一段时间重写并执行相同查询。按回执提示组织有界等待，并在完成或需要介入时通知 Main，具体见 [background-wait.md](background-wait.md)。MCP worker 负责观察网页生成，不因进度不变重复发送。

只有 `COMPLETE` 时使用 `chatgpt_result` 获取完整回答、来源、URL 与实际选择证据。大结果若当前实例支持 reference delivery / `chatgpt_result_page`，按其回执分页到 EOF 并核对长度和哈希；不能把客户端截断的 inline 内容当成完整回答。会话归档另见 [export.md](export.md)。

支持独立研究工作流的实例会返回 `research`：`expected` 只表明预期研究，`observed` 表明已观察到本轮研究 UI，`report_complete` 是报告归属、完成信号和正文的证据。外层“研究已启动”、进度卡片或标题不构成最终报告；即使请求模式是 consult/critic，实际进入研究也应继续等待同一 task。报告就绪后顶层可能短暂仍为 RUNNING，只有顶层 COMPLETE 才取结果。技术完成不证明资料或引用正确，文件证据按 [file-evidence.md](file-evidence.md) 核验。

## Oracle 反问与页面接管

`NEEDS_USER_INPUT` 是服务状态，不代表每个问题都必须询问人类。读取 `chatgpt_status` 或 `chatgpt_takeover` 的实际回执；提供新字段的实例按 `user_input_kind` 判断：

| kind | 处理 |
| --- | --- |
| `content_clarification` | 读取 `clarification_text`，结合当前用户/助手 message ID 核对这是本轮已停止生成的内容问题。Main 能根据本地事实、现有授权或合理条件分支回答时，用同一 Session 的续聊工具、新 `request_id` 回复，产生新 turn；不重发原问题 |
| `manual_browser` | 按 instructions 与页面证据处理；登录、CAPTCHA、账号保护或 OAuth 授权按 [security.md](security.md) 交给人工 |
| `composer_draft` | `COMPOSER_DRAFT_PRESENT` 表示已有草稿被保留，不能自动清空或覆盖。让草稿持有人处理，先核对原 task 的发送证据再决定如何继续 |

内容澄清涉及用户独有目标、价值选择、偏好、授权或未知事实时再询问用户。`clarification_text` 不把任务变成 COMPLETE，不能据此调用 `chatgpt_result` 获取完成结果；普通问题可由 Main 回复，不把可自行回答的技术或现状问题转交用户。

先区分实际要求人工完成的登录／一次性 CAPTCHA，与普通运行时错误。站点告警中的 `cloudflare_challenge` 错误码本身不证明出现了人工验证题；服务端应保留原页面、task 与发送回执，在原任务内有界恢复。只在网页确实要求登录或人工验证时使用 `NEEDS_USER_INPUT / manual_browser`，不能因错误码把日常维护交给用户。用户或助手正文讨论这些词也不构成站点告警。

恢复只能使用网页正常提供的流程：发送前的空白新页可刷新／重建；已发送任务先核对正式会话、当前用户回合与正文 hash，确认无新回答、无后续用户问题、无人工草稿及生成中状态，才能尝试当前回合的“重试”或重新加载原会话。每次动作前保存恢复意图，重启不重置动作预算，不创建新问题、不换 request_id。无法确认动作结果或达到恢复上限时返回带证据的服务错误；不无限重试，也不把恢复耗尽称为需要人类验证码。

支持此恢复的实例对同一 task 最多准备两次运行时动作：第一次优先使用匹配告警的“重试”，后续只重新加载原正式会话。`runtime_recovery_attempts` 与 `runtime_recovery_actions` 记录已消费预算和动作意图；`prepared_at` 不证明动作已生效。对外任务仍为 `RUNNING`，progress 会说明正在恢复；客户端继续等待同一 task。`RUNTIME_RECOVERY_*` 错误保存现场并交 Main 排查，不能据此重提原问题。

这些错误码只表示“本轮页面出现了可观察的运行异常”，不代表问题未发送，也不改变原回合身份：
- `site_conversation_load_failed`：页面显示“无法加载此 ChatGPT 对话 / 再试一次”错误卡片；首次恢复点卡片自身的“再试一次”，不当作助手回答或人工验证。
- `conversation_route_left`：已发送的标签页被带回 ChatGPT 首页；恢复动作是重新打开原正式会话地址（只读，不新建 turn）。
- `provisional_uncommitted`：发送后会话始终停在 `local-chatgpt:` 临时地址，超过短观察窗口（默认最多 180 秒）仍未被服务器确认；提前转入恢复，不再耗尽整个观察预算。恢复同样只重载原地址；若仍未出现可关联回答，任务以超时结束并保留现场。

## 页面诊断

实例若提供 `chatgpt_diagnostics(task_id)`，返回的三类证据只用于判断页面当时发生了什么，不是回答内容：`page_probe_events`（标签页关闭后仍保留的控制台报错、失败请求、后端 5xx、页面级运行告警；已去掉域名、`verify`/`token` 一类查询值与正文）、`submission_events`（发送前后的准备与恢复里程碑）、`runtime_recovery_actions`。任务卡住或超时后先看这里，再决定是否需要人工；不能据此重发原问题。

缺字段、内容为空或发送身份不明时，先通过匹配回合的只读证据核查；状态标签或泛化 takeover 提示不替代实际问题。无法可靠取得本轮内容时保留缺口，不引用旧回答假装澄清已读到，也不追加原问题来试探。导出在登录/验证阻塞时按其 instructions 交给人工。

## 取消确认

调用 `chatgpt_cancel(task_id)` 后核对终态与资源状态。`CANCEL_REQUESTED` 只是取消意图，不代表完成，也不代表队列或页面已释放。

`cancellation_stop_verified=true` 是网页停止的验证证据。若为 false 或返回 `CANCEL_STOP_UNCONFIRMED`，本地操作与自有页面已回收不表示 ChatGPT 云端生成已停止；保留原身份与 URL，先只读核查。旧实例未提供字段时不能声称停止已验证。

取消、关闭持续不结束或 HTTP 超时时，记录原任务、Session、状态与时间，不能由网络超时推断清理完成，不无限轮询或再次发送问题。导出取消使用 `chatgpt_export_cancel`，而不是对排队/运行中的导出调用 release。

## 故障解释

| 状态/错误 | 应对依据 |
| --- | --- |
| `BROWSER_NOT_READY` | 自有空白新页在一个总预算内尝试就绪检查、一次刷新及必要的一次关闭重建；耗尽预算后停止。草稿、附件、既有对话、登录/验证及已尝试发送的页不走此恢复路径 |
| `MODEL_UNAVAILABLE` | 所请求档位不可用，保持档位语义，不自动降级 |
| `BROWSER_UNRESPONSIVE` | 浏览器调用超出期限；命名失败 Session / 未确认发送尽可能保留自有页和回执，不证明问题未发送 |
| `BROWSER_LOCK_TIMEOUT` | 浏览器资源锁等待预算耗尽；先查询原任务状态 |
| `BROWSER_UI_BUSY` 且 `state=QUEUED` | 初始交互锁暂被其他任务占用；后台等待同一任务，服务延后准入，不另提交问题 |
| `BROWSER_UI_WAIT_TIMEOUT` | 初始交互准入已超过任务等待预算；保留原任务与诊断，由 Main 判断下一步，不自动重提 |
| `COMPOSER_DRAFT_PRESENT` | 发送前发现已有草稿，保留草稿并停止覆盖；核对 `user_input_kind` 与发送回执 |
| `COMPOSER_PREPARATION_FAILED` | 服务自己的输入准备未确认成功；属于服务故障，不能改称人工登录或验证码。保留 task 与发送证据，由 Main 排查；不因错误码自行换身份重发 |
| `RESEARCH_REPORT_WAIT_TIMEOUT` | 研究观察预算耗尽，尚未取得匹配且完成的报告；保留原 task / URL，不把静止卡片作为答案，不据此声称云端已停止 |
| `ACCOUNT_PAGE_UNAVAILABLE` | 偶发账户加载失败不等于 logout。自有空白新页按上述有界刷新/重建恢复；已有对话与草稿保留，耗尽预算后核查原身份，不重提问题。logout、登录、CAPTCHA 交给人工 |
| `BROWSER_ERROR` | 保留任务、回执与现场证据，结合实际执行顺序定位；最后 progress 不能单独确定失败阶段 |
| `TIMEOUT`、`SEND_UNCONFIRMED`、`TURN_IDENTITY_UNAVAILABLE` | 保留原身份与 URL，按发送证据只读核查，不自动重提原问题 |

capabilities 成功不证明任务操作的页面已就绪。服务故障、模型语义和连接层故障应分开解释，参见 [connection.md](connection.md)。
