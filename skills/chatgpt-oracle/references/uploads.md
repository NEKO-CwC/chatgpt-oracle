# 文件上传与回传

在给 Web 提供上下文文件或发回选定附件时读取。证据判断见 [file-evidence.md](file-evidence.md)，范围与凭据见 [security.md](security.md)。

本页为 ChatGPT 上传与回传说明；Gemini 的普通文件输入、tus/file_ref 与 ZIP 拒绝边界见 [gemini.md](gemini.md)。

## 已验证的直接上传链路

1. 向 Remote MCP 服务的 `POST /uploads` 上传原始字节，使用 `X-Upload-Filename` 和 `Content-Type`，保存返回的短期 `upload_id`，核对文件名、大小与 SHA-256。
2. 在 `chatgpt_submit`、`chatgpt_continue` 或 `chatgpt_resume` 传 `upload_ids=[...]`。不要把大文件编码进 prompt，也不要用本机路径替代服务端句柄。
3. 核对任务 receipt 的附件信息与网页附件验证。文件在服务端内存中短暂保留，完成、取消、失败或过期后释放；receipt 保留元数据与验证结果，不包含正文。

当前实例也可能提供分块上传、`files` / `file_ref` 和 transfer 工具；先核对 `tools/list` 与 `chatgpt_capabilities(scope="service")`，按该工具回执使用。`upload_id`、下载句柄与 `file_ref` 不是同一种身份；不要混用，或在同一请求中同时提交相互排斥的上传参数。

## 页面与读取证据

上传阶段可能显示 `UPLOADING`、`ATTACHMENTS_VERIFIED`、`SEND_PENDING`。浏览器上传在对应 Session 队列内执行，最后 progress 不单独证明执行顺序或故障根因。上传后还需核对实际档位，见 [runtime.md](runtime.md)。

`POST /uploads` 成功只证明服务收到字节；页面附件 `verified` 证明网页附件验证成功。模型确实读取需回答中的具体内容证据。ZIP 验收要求引用其中两处没有写进 prompt 的具体内容，并对照解压后的源文件逐项核对。

`UPLOAD_ERROR` 后先核对原任务是否已发送。若已确认需要另建任务，使用新上传句柄与新 `request_id`，不复用终态任务释放的句柄；查询或网络重试原任务仍使用原身份，不自动另建任务重发。

## 补充文件

图片细节不足时，说明对应文件、消息、具体区域和待识别内容，再选择原文件、完整下载表示或更清晰的局部图。这是可选能力，不自动触发升级档位、重新研究或重发整个会话。

只有需要 Web 继续处理时，才将选定字节重新上传，并在同一 Session 续聊中注明来源是原文件、派生图或局部图。只需本地识别时交给客户端实际支持的文件/图片输入能力。下载句柄不能当作 `upload_id`。

同一 SHA-256 的文件重复下载、改名或发回不会增加细节，裁剪或放大不会恢复丢失像素。分别确认字节交付、页面附件验证与先前指定区域的识别结果，仍不清楚时保留具体缺口。
