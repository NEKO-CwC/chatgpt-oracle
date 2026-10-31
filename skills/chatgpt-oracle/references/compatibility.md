# 实例能力与历史兼容

在首次操作当前实例、升级或遇到字段缺失时读取。历史验收说明已验证的范围，不保证任意实例或当前页面具有同样能力。

下述 `chatgpt_*` 工具与早期验收针对 ChatGPT，共享浏览器字段适用于两者。Gemini 的 27 工具、档位、文本快照及附件限制见 [gemini.md](gemini.md)；两者通过 [connection.md](connection.md) 选择各自入口，不跨提供方套用模型或附件验收结论。

## 当前实例检查

先核对 MCP `tools/list` 的实际工具和参数，再用 `chatgpt_capabilities(scope="service")` 检查需要的能力：

- `browser.max_sessions=4`、`operations_per_session=1`、`tabs_retained_until="session_close_or_expiry"`：四 Session 并发、各 Session 串行与原页驻留契约。
- `browser.pool_scope="shared_profile"`、`shared_providers=["chatgpt","gemini"]`：一个 Profile / CDP 的两提供方合计四槽位。健康状态的 `session_count` 为全局合计，`local_session_count` 为当前提供方；各自的任务/导出/文件存储独立。
- `turn_receipts.enabled`、`turn_receipts.completion="matched_current_turn"`、`cancel_interrupts_reads`、`stop_verification_reported`：本轮身份与取消验证契约；结合实际任务回执使用。
- 按需核对 upload / download / result_pages 等传输能力，不把某个字段存在当成真实链路成功。
- `composer_preparation.native_editor_transactions`、`cleanup_verification="editor_model_and_dom"`：支持原生编辑器准备与模型/DOM 双重清理校验；不表示可以覆盖未知草稿。
- `research_workflow.completion="matched_completed_report"` 与 `native_inner_documents`：独立研究文档的完成和结果读取契约，实际阶段查 task 的 `research`，含义见 [runtime.md](runtime.md)。

若实例报告 `api_revision=4` 与 `oracle_protocol`，核对所需协议字段，而不是仅据版本号推断：

- `version="oracle-v1"`、`origin_marker="【CODEX_ORACLE】"`、`marker_enforced=true`：最终发送层的首行标记契约。
- `bootstrap_version="oracle-bootstrap-v1"`、`bootstrap_delivery="confirmed_user_turn"`：服务端 Core 版本及送达证据类型；实际 task / Session 是否确认仍查回执。
- `new_chat_kind="ordinary"`、`memory_policy="prompt_only"`：普通 New Chat 与 Memory 软约定，不是账号记忆隔离保证。
- `draft_policy="reject_nonempty"`：已有 composer 草稿不覆盖；结合 `COMPOSER_DRAFT_PRESENT` 和实际回执处理。

任务回执的 `oracle_protocol.bootstrap`、Session 配置与确认的区别见 [task-identity.md](task-identity.md)。`user_input_kind` / `clarification_text` 存在时按 [runtime.md](runtime.md) 区分内容问题、人工浏览器操作和草稿；缺字段时不能从泛化 takeover 文本假造本轮问题。

`chatgpt_capabilities(scope="browser")` 用于当前账号可见模型和能力，与 service scope 不同。它可能操作诊断页面；成功不证明随后任务页面准备完成。

若 service capabilities 报告 `browser_diagnostics.admission="independent_temporary_session"`，浏览器诊断使用独立临时槽位，不借用已有 Session，包括本地 TIMEOUT 但上游可能仍生成的页面。四槽位／维护门不允许时，返回结构化 `state="BUSY"`、`error.code="BROWSER_DIAGNOSTIC_BUSY"`、`retryable=true`，无需等 30 秒。客户端按退避稍后查询即可；该诊断 BUSY 不改变原问答任务状态，不据此重发、关闭在途 Session 或判定模型不可用。`scope="service"` 不进入浏览器队列。

`wait_budgets_seconds` 是服务观察预算，不是上游生成停止时间。`TIMEOUT / GENERATION_WAIT_TIMEOUT` 表示最后一次观察网页仍标记进行中，`TIMEOUT / TURN_RESULT_WAIT_TIMEOUT` 表示尚未取得可关联的正式助手回答；两者保留已确认发送的原身份，不自动停止或重发。新版活动卡片的 `in_progress` 与思考摘要不等于完整回复；仍需本轮助手身份和正式内容。

服务实现与 MCP SDK 分别核对：`mcp==1.30.0` 是 SDK 版本，不证明服务含某次修复；服务健康版本 `0.1.0` 本身也不足以识别这些修复。以工具目录、能力/回执字段与对应实例验收证据判断。

## 已验证范围

- 2026-10-03 的部署验收覆盖四个命名 Session 并发、原页续聊和 ZIP / critic / Extra High 链路。
- 2026-10-05 的续聊修复验收覆盖新 critic / Extra High 首问与 6634 字续问 COMPLETE、同一物理 target、取消约 1.114 秒且 stop verified，以及不同会话 URL 的导出同时运行并到 READY / RELEASED。该轮导出只读取附件清单，不证明下载全部附件字节；未测页面 TimeOrigin。
- 2026-10-06 的 oracle-v1 验收覆盖新 ZIP / critic / Extra High 首问、首行 marker 与一次 Core、两个文件内原句、两次新反驳续聊 COMPLETE、原页复用和幂等返回原 task。消息分页 37 页、Markdown 9 页与附件清单 1 页完成重组和 hash 核验；原件下载仍 partial。三份 fresh Codex/Web 对照及同页分歧续聊完成，服务重启后保留原 target。Memory 写入没有被验证。
- 2026-10-14 的 ChatGPT 附件修复验收覆盖 Tailscale 免 Bearer 下的 tus/file_ref ZIP 首问（8 张 PNG 与校验文本），以及旧 `POST /uploads` TXT 同页续聊；两轮均为 Extra High、发送 confirmed、结果 COMPLETE，读对未写进 prompt 的独立校验串。ZIP 回答列出 8 个文件名，并正确描述前两张图的颜色和形状；不外推为全部复杂截图的审阅质量。全量回归 400 通过 / 3 跳过，测试 Session 与输入引用已释放，用户原审阅批次未代发。
- 2026-10-15 的 Markdown 回合适配覆盖 3541 字、含反引号和加粗语法的真实首问，发送 confirmed、回答 COMPLETE，测试 Session 已关闭。另以 DOM 消息 ID 绑定的完整原文和精确哈希，只读恢复一条 `SEND_UNCONFIRMED` 的原首轮结果，未新增或重发消息，后续网页续聊未混入该 task。最终相关回归 62 通过；正文显示相似不替代身份依据。
- 2026-10-15 的共享浏览器验收确认两者同 Profile/CDP、均保持登录；真实共享账本中 2+2 驻留占满、第五排队、已有 Session 可继续、释放后第五获准。实际原生 Chrome 的跨提供方页面峰值为四，第五页拒绝；原有页保留，断开验证客户端不关闭共享 Chrome。此轮未向模型发送问题；不证明上游四路同时生成。全量 412 通过 / 3 跳过，后续配置与共享池聚焦 75 通过。
- 本轮身份字段缺失的旧实例不能默认具备新回合确认或取消停止验证；保留对应不确定性，不用旧回答补成成功。

这些是历史部署的证据，更新 Skill 不升级服务实例，也不替代当前能力检查或任务回执。

旧实例未提供上述协议字段时，客户端仍保持首行 marker，但不能声称服务最终发送层兜底、Core 已送达或草稿保护已具备；不复制整份 Core 来伪造升级或补填旧回执。新协议是否在所连实例部署和通过真实首问/续聊验收，应使用该实例的新证据判断。

## 兼容入口与限制

旧 inline `chatgpt_export`、旧 manifest 的下载 URL 与直接 `POST /uploads` 仍需按所连实例的工具/路由能力使用；长会话优先分页工具。下载、上传和文件引用句柄不通用。

当前 Library ZIP/Markdown 原件下载存在缺口；图片 pointer 与原件分开报告，见 [file-evidence.md](file-evidence.md)。DOM fallback 的导出可能 unverified；缺少可靠发送锚点时应明确不确定，不重放问题。

2026-10-28/29 的真实研究测试出现过文件名正确、报告却编造两条文件引文的情况，原 ZIP 与派生 TXT 的内容识别未因此通过。此记录不证明所有模型都不能读这些格式，也不能仅凭它确定传输、工具转发或模型能力哪层出错；它说明历史上传验收与当前报告自述均不能替代本次原件比较。完整性核验规则见 [file-evidence.md](file-evidence.md)。

该测试的同页普通续聊随后逐字读对了两份 TXT，并撤回研究报告的错误来源声明。这证明那些 TXT 在普通续聊中可读，仍不证明独立研究阶段实际使用了附件，或模型直接读取、解包了 ZIP。

登录/CAPTCHA 由人工处理。FRP/TLS 与 IP Host 421 是独立连接现象，历史网页修复未解决它们；没有长期内存压力验收或原始 OOM 的确定归因。

## 安装与分发

Skill 由 `SKILL.md` 与十份 `references/` 共十一个文件组成，包含 Gemini 独立操作说明。安装或发给远程客户端时同步整个 `chatgpt-oracle/` 目录，保持相对路径，并刷新技能缓存；只发主文件会丢失操作手册。完整 Core 属于服务安装资源，不放入客户端目录。MCP 连接配置与服务版本独立于 Skill；更新 Skill 不启动服务或配置远程入口。
