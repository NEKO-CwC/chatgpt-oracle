# 会话分页导出与附件下载

在回顾、归档或读取既有会话附件时读取。正文、清单、文件与实际阅读的证据区别见 [file-evidence.md](file-evidence.md)。

本页为 ChatGPT 分页与附件下载说明；Gemini 仅交付已观察文本快照，无源附件下载工具，见 [gemini.md](gemini.md)。

## 快照与分页

1. 调用 `chatgpt_export_start`，优先传已有 `session_id`，也可传 `task_id` 或 `conversation_url`。`scope` 默认为 `all`；只需最近回合时传 `scope="recent", count=N`。保存返回的 `session_id`、`export_id` 与请求身份。
2. 用 `chatgpt_export_status(session_id, export_id)` 等到 `READY`，按 `retry_after_seconds` 查询；建议使用一次启动的持续后台脚本，见 [background-wait.md](background-wait.md)。若为 `NEEDS_USER_INPUT`，结合 instructions 按 [runtime.md](runtime.md) 处理；登录与验证由人工完成。`READY` 只表示快照可读。
3. 分别检查 `completeness.messages`、`completeness.attachments` 与 `overall`。messages 的 verified 不代表附件完整；附件库存/来源状态不代表客户端已经下载或读取文件。partial / unverified 要连同数量、source 和具体缺口报告。含内部 assistant/tool 消息的 `message_count` 不能当成问答轮数。
4. 对需要的 view（`messages`、`attachments`、按需 `markdown`）使用 `chatgpt_export_page`：首请求 `cursor=null`，后续原样复用 `next_cursor` 直到 `eof=true`。按回执格式保持页序、重组内容，再核对 `total_bytes` 和 SHA-256。不能只凭 READY 或 message_count 声称已完整读取。

按任务需要读取 view；只需正文时不强制下载所有附件。消息关联、分页完整与内容识别分别报告。

导出保留原始角色、message ID 与正文中的 marker，包括引用内容；网页 `user` 不等于已认证的人类。服务发送来源按 [task-identity.md](task-identity.md) 用匹配回执关联，缺依据时保持未知，不因 marker 有无推导身份或授权。

提供来源字段的实例在消息视图附加 `origin`。`evidence="matched_service_turn_receipt"` 表示用户 turn ID 与实际发送正文 hash 匹配。DOM 临时 ID 被 API UUID 替代时，只有完整且已验证的全量 API 快照、唯一正文与唯一已确认服务回执三者同时具备，才可返回 `confirmed_service_turn_unique_text`；分别保留回执 `user_message_id` 与 `export_message_id`。重复正文或多个候选回执保持未知。

`marker_present` 只报告首行文本约定；`source="service_codex"` 依赖上述证据，不认证人类身份。无法关联的历史保持 `source="unknown"`。来源元数据不改变自然 Markdown。

快照是 immutable：来源解释与任务回执单独保存，不改写既有正文、角色、附件证据或 hash。只读导出不会补发 bootstrap 或恢复提醒。

## 附件字节

已授权任务需要查看或传输附件时，对 manifest 中 `available` 或 `download_candidate` 的项调用 `chatgpt_export_attachment_handle(session_id, export_id, attachment_id)`。

`download_candidate` 仅表示将通过对应 Session 的浏览器操作尝试文件卡片动作，不代表文件一定可用。按 [连接说明](connection.md) 的入口认证规则 GET 返回的 `download_url`（Tailscale 直连免 Bearer，公网需 Bearer），核对 HTTP `Content-Length` 与 `X-Content-SHA256`；句柄过期或消费后重新领取。下载句柄与上传 `upload_id` 不通用。

V2 附件视图不返回源 URL 或签名 URL。最终 unavailable 项保留并说明具体缺口，不宣称附件全量。Library 原件与图片 pointer 的区别见 [file-evidence.md](file-evidence.md)。

## 释放与兼容

完成后调用 `chatgpt_export_release(session_id, export_id)`。`state=RELEASED, accepted=false, error_code=null` 是正常释放回执；不能将 accepted=false 一律当成失败。排队或运行中的导出使用 `chatgpt_export_cancel`，按状态与错误码核对结果。

`EXPORT_GONE` 后旧游标不能继续使用；如仍需导出，以同一 Session 和新 `request_id` 创建新快照。显式关闭 Session 会撤销其快照；释放导出本身不是关闭仍需续聊的 Session。

旧 `chatgpt_export` 是 inline 兼容入口。长会话使用分页工具，避免单次 MCP 输出被客户端截断。只需归档而不要求模型阅读时，可按入口认证规则使用 `POST /exports/{export_id}/file`，请求体传 `session_id` 与 `view`，流式取回文件；这不表示模型逐页读过内容。
