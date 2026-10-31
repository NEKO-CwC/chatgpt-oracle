# 请求、任务与当前回合身份

在提交、续聊、网络重试或发送状态不明时读取。实际参数以当前 `tools/list` 为准。

本页工具与详细回执针对 ChatGPT。Gemini 共用保留请求身份与不重复发送的原则，Deep Research 额外用户 turn 的关联另见 [gemini.md](gemini.md)。

## 提交与幂等

按主 Skill 的语义快照准备 prompt；对开放 Consult 不默认附加实现计划、checklist 或批准要求。`chatgpt_submit` 接受 `mode`、`prompt`，以及按需的 `session_id`、`project`、`quality`、`model`、`request_id` 等参数。

- 每轮新问题使用新的 `request_id`；有意改变 prompt 或参数也是新请求。
- 网络重试同一请求时复用原 `request_id` 与原参数。已有 `task_id` 后查询该任务，不重新提交 prompt。
- 不因等待、进度停滞、浏览器错误、超时或连接中断生成新身份重发同一问题。
- 保存 `session_id`、`request_id`、`task_id`、请求档位与确认后的 `conversation_url`。句柄、档位和取消分别见 [uploads.md](uploads.md)、[runtime.md](runtime.md)。

支持 `oracle-v1` 的服务在发送前固定最终正文：首行 marker、按需的 Core / 短提示与调用者内容。原始调用输入与参数继续决定幂等身份；同参重试返回原 task，不因 Session 后来已有 bootstrap、改变 mode 或更新服务资源而重新组合、重发。

`prompt_sha256` 对应原始 prompt；`oracle_protocol.sending_text_sha256` 对应固定的最终正文；`sending_text_normalized_sha256` 与浏览器的 `submitted_prompt_sha256` 对应空白规范化后的实际发送文本，用于匹配网页回合。这些 hash 用途不同，不能用未包装 prompt 的 hash 替代发送正文身份。

## 回执分别证明什么

| 字段或状态 | 证据含义 |
| --- | --- |
| `resolved_model`、`resolved_reasoning_effort`、`selection_verified_at` | 模型/档位解析与选择验证，不单独证明问题已发送 |
| `submission_state`、`submission_verified_at`、`current_user_message_id` | 当前问题的发送关联证据；缺字段时保留发送不确定性 |
| `current_assistant_message_id` | 用于关联本轮回答；结合当前用户身份与任务状态核对 |
| `RUNNING` 或 progress | 服务正在处理或观察；不能独立证明发送或新回答已经发生 |
| `COMPLETE` 与本轮身份匹配 | 可取回本轮结果；不能用历史回答的标题、节点数量或旧内容代替本轮关联 |
| `oracle_protocol.version`、`marker_enforced`、`prepared_at` | 本 task 的组合版本、固定正文首行与准备时间；不单独证明已送达 |
| `oracle_protocol.bootstrap.state="prepared"` | 固定正文已包含 Core，尚无已确认用户 turn 的送达证据 |
| `oracle_protocol.bootstrap.state="confirmed"` 及 `task_id`、`user_message_id`、`verified_at` | 含 Core 的首轮 task / 用户 turn 与确认时间；后续任务可引用该依据，不表示本轮重复包含 Core |
| `oracle_protocol.bootstrap.state="unknown"` | 未取得可靠的 Core 送达依据；旧记录或已有 URL 不能补造确认 |

服务在点击发送前持久化 `submitted_prompt_sha256`、用户/助手身份基线、档位和附件回执；`SEND_PENDING / unconfirmed` 也可能只是准备回执，不能据此声称已点击或送达。URL 等待失败仍保留这些字段，首页或临时 URL 不建立正式对话映射。历史消息可能虚拟化，渲染的 fallback 索引也可能复用；单凭可见助手消息数量增加或某个索引不能确认当前回合。

短确认窗口结束后，支持原页观察的实例继续保持 `SEND_PENDING / unconfirmed`，在任务总等待预算内只读观察同一页面；正式 URL 和当前用户回合匹配后才进入已确认状态。响应晚到不新建任务、不再次点击发送，也不因暂时没有 URL 直接判成终态错误。明确错误、页面丢失、人工接管或总等待耗尽仍按实际回执处理。

ChatGPT 可能把用户消息渲染为 Markdown 并加入“显示更多”等界面文字。支持该适配的实例使用与该条 DOM 消息 ID 绑定的渲染器原文核对发送哈希；不通过删除 Markdown 标点或近似文本匹配来确认。缺少原文绑定、哈希不符或多个候选时仍保留未确认状态。

`SEND_UNCONFIRMED` 表示一次发送后的有界观察未确认本轮身份；`TURN_IDENTITY_UNAVAILABLE` 表示恢复的旧任务缺少可靠的本轮身份。两者均不证明未发送。保留原身份和 URL，先只读核查已有会话，不换 `request_id` 重提原问题。

正常的新一轮反问回答是有意追加内容，应使用新身份；它与对不确定发送的原问题重试不同。是否回答 Oracle 的内容问题遵循主 Skill，账号和授权问题遵循 [security.md](security.md)。

## 来源与 bootstrap 依据

`oracle_protocol.mode` 是本 Job 的模式；`bootstrap.included_this_turn`、`mode_overlay_included`、`recovery_reminder_included` 分别说明本轮固定正文包含什么。续聊的 bootstrap `confirmed` 可以引用先前首轮，仍需用本轮 `submission_state` 与 `current_user_message_id` 核对新内容是否已发送。

Session 的 `configured_version` / `configured_bootstrap_version` 是协议意图，不能解释为送达。同一 URL 的别名可共享首轮确认依据；来源保持 task / 用户 turn，而不是逻辑 Session 名称。

回收来源时保存原始角色、message ID 与 marker，用服务账本及匹配回执关联服务发送的 turn。复制的 marker、`user` 角色或无 marker 都不是认证；不能关联的来源保持未知，更不能从这些文本推导用户授权。导出处理见 [export.md](export.md)。
