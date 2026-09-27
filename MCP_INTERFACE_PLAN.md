# MCP 接口与大文件传输设计（2026-09-26）

状态（2026-09-26）：I0–I3 文件通道、视图磁盘化、域约束 cookie 的 HTTPX 流和源 API 页级 spool 已实现；全量 pytest 为 126 passed、1 个默认 opt-in skip，`tus-js-client@4.3.1` 独立互操作通过。目标会话 stream_pages 烟测为 2 API 页、65 raw/63 visible，106 个结构化消息页重组 SHA 匹配；25 项附件中 22 项有来源/候选，之前同版附件链已逐项验证 7,270,631 bytes，3 个 Library 原件无来源、附件 partial。256 MiB TUS+完整慢读通过，写失败回滚和多进程 offset 争用有回归。仍有单响应页/单消息峰值、spool+snapshot 组合磁盘峰值和真实 OS 磁盘满未验收；真实客户端识图、FRP/公网及 I4 发布未验收。生产 Broker 未重启。

## 1. 当前证据与改造范围

| 当前实现 | 已核对的代码 | 影响 |
| --- | --- | --- |
| 上传逐块接收，最终 `chunks.append` + `b"".join(chunks)` | `uploads.py:96–171` | HTTP 接收分块不等于有界内存，合并阶段还会同时持有多份数据 |
| 附件先 `response.body()`，JobManager 返回 `bytes`，HTTP 再切 64 KiB | `browser.py:2894,2970,3039`、`jobs.py:930`、`server.py:708` | 当前没有端到端的有界内存下载 |
| 浏览器上传把每个文件放进 `buffer` | `browser.py:3041` | 只改 HTTP 层仍会在 Python/Playwright 桥接层整文件缓冲 |
| 默认单文件 25 MiB、legacy 上传总内存 64 MiB、保留快照磁盘 64 MiB | `config.py:77–85` | Broker 的本地资源上限，不能当成 ChatGPT 上游支持的文件大小；源页聚合阶段仍有内存峰值 |
| 下载前即消费一次性句柄 | `jobs.py:933`、`exports.py:923` | 首次读取失败也会消费句柄；该语义不能承载 HEAD、多次 GET 或 Range 续传 |
| 源 API 页逐页写临时 spool，视图从 spool 生成磁盘文件 | `spooling.py`、`exports.py`、`browser.py` 的 `stream_pages` 分支 | 整段消息数组不再同驻内存；仍同时持有单个完整 API JSON 页与其归一化表示，单页/单消息峰值及组合磁盘预算待压力验收 |
| `chatgpt_result` 整段返回正文 | `server.py:227` | 长结果仍可能超过客户端可见输出上限 |
| stdio 的 export 调用代理到 HTTP；submit/resume 使用本进程 manager，UploadStore 为进程内对象 | `server.py:81,149,184,341`、`jobs.py:71` | 文件状态跨入口不一致是代码层已确认的设计缺口；本轮未把它写成已复现的生产故障 |
| 一般任务幂等查询只匹配 request_id；导出另有输入冲突校验 | `scheduler.py:611–629`、`jobs.py:442` | 新文件引用加入任务后，需要统一幂等输入摘要，避免变更输入被当作同一次请求 |

## 2. 参考及复用边界

已于 2026-09-26 读取以下官方资料。先采用成熟的协议语义，再按当前 Python/ASGI 架构选择最小实现。

| 参考 | 采用的部分 | 不据此承诺的部分 |
| --- | --- | --- |
| [tus 1.0 resumable upload](https://tus.io/protocols/resumable-upload) | Creation、HEAD 查询 Upload-Offset、PATCH 顺序续传、偏移冲突 409、Expiration/Termination；校验扩展按实现能力广告 | 不宣称已兼容 tus；不在首版实现并行 Concatenation、未知总长或所有扩展 |
| [HTTP Semantics / RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#section-14) | GET/HEAD、Range、If-Range、强 ETag、206、Content-Range、416 | 上游 ChatGPT 是否支持 Range 必须单独实测；不能把本地暂存支持续传等同于上游支持 |
| [HTTPX async streaming](https://www.python-httpx.org/async/) | AsyncClient.stream、逐块读取、取消时关闭流、背压 | 不能假设已观察的内容 URL 在无浏览器上下文时一定可读取 |
| [Playwright FileChooser](https://playwright.dev/python/docs/api/class-filechooser) | 使用服务端暂存文件路径调用 set_files，复用现有附件验证 | 路径上传不证明 ChatGPT 最终给模型呈现原始像素；不接受客户端指定服务器任意路径 |
| [MCP 2025-11-25 transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) | JSON-RPC 控制消息与已协商版本的传输能力 | Streamable HTTP/SSE 本身不是二进制文件续传协议 |

独立协议验收使用官方 `tus-js-client@4.3.1`；包只安装在测试临时目录，不加入 Python 运行依赖或 Node 前端工程。服务端广告 Creation/Termination，未实现未知总长、并行 Concatenation 与 Checksum 扩展，OPTIONS 不会声称支持这些扩展。

I0 组件筛选记录（2026-09-26）：仓库固定依赖仅为 MCP 1.30.0 和 Playwright 1.58.0，没有已安装 tus 服务端。`bartscherer/tussi`（MIT、ASGI-native）文档明确标记 Linux-only，并依赖 `posix_fallocate`/`fcntl.flock`，当前 Windows/Chrome 环境不能采用。`liviaerxin/fastapi-tusd`（MIT）是 FastAPI 路由而项目没有 FastAPI，作者标出的 Expiration 与 Checksum 未实现，最近提交为 2025-06-03。`kirill-ilichev/TusFastAPIServer` 声明无许可证，排除复制和依赖。当前也没有可验证候选能同时满足 Windows、所需生命周期、单 Broker ASGI 集成和有限生产依赖。按下文门槛，在现有 ASGI app 实现 tus 1.0 core + Creation/Termination 子集；用独立 tus 客户端互操作验证，不添加生产 sidecar。

环境核对：项目虚拟环境 MCP 为 1.30.0，支持的协议版本截至 2025-11-25。官方 latest 当前指向 [2026-07-28 Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)，其中 Last-Event-ID 和 MCP 传输 session 语义已有变化。本轮不顺带升级 MCP 协议；业务 session_id、任务和文件恢复不依赖某版传输 session 或 SSE 自动重放。

协议实现选型 I0 已完成：在现有 ASGI 文件通道实现 tus 标准子集，以固定版官方 tus-js-client 独立验收；该包仅安装在测试临时目录，不进入 Python/生产依赖。实现不使用额外 Go/Node 服务或自创偏移协议。

## 3. 总体结构与标识

MCP 管任务、元数据、分页和文件引用；受保护 HTTP 通道管二进制。stdio 与 Streamable HTTP 是同一个 Broker 的入口，所有有状态操作共享调度、FileStore 和 transfer 元数据，stdio 不再另持一份上传内存。

```text
Agent / 客户端
  ├─ MCP：session / task / export / file_ref / transfer_id
  └─ HTTP：tus 上传、GET/HEAD + Range 下载
                 ↓
        同一个 Broker / FileStore
          ├─ 小文件内存后端
          ├─ 大文件限额暂存后端
          └─ 浏览器适配器：取源、上传、确认消息归属
```

- `session_id`：业务上下文；SessionStore 继续只保存 URL、任务引用、TTL。不同客户端要协作时显式使用同一 session。
- `export_id`：不可变会话快照；每个 view 有独立游标和摘要。
- `result_id`：冻结的任务结果版本；长结果分页不随新追问变化。
- `file_ref`：Broker 文件引用，和 OpenAI 的 file_id / library_file_id 区分。READY 后代表固定字节；图片表示变化必须得到新的引用，不覆盖旧字节。
- `transfer_id`：上传接收或源文件准备操作；断线恢复、查询、取消围绕它进行。文件可在有效期内多次读取，transfer READY 不表示客户端已经读取或模型已经看懂。
- 所有新文件/传输工具带 `session_id`，服务端验证归属。会话和文件引用不是身份凭据；当前共享 Bearer 也不等于多租户隔离。

## 4. 保留与修改现有 MCP 工具

| 工具组 | 设计变更 |
| --- | --- |
| `chatgpt_session_create/status/close`、`chatgpt_sessions` | 保留命名、精确 URL 绑定和关闭标签后 resume 重开原 URL 的语义；显式 close 继续撤销该 session 的导出快照。已物化 file_ref 按独立 TTL/release 管理，活动任务的文件租约由任务释放；不同 URL/不匹配 session 的引用拒绝使用 |
| `chatgpt_submit/resume/continue` | 增加 `files=[{file_ref, relates_to?, note?}]`，关联原消息/附件与待识别区域；只接收 READY 文件。保留 upload_ids 兼容入口，同一请求只使用一种输入形式。服务端验证文件归属、大小和可上传性 |
| `chatgpt_status/cancel/takeover` | 保留；status 增加结构化 phase/progress 和关联 transfer IDs；cancel 取消本任务的文件占用和浏览器操作。已共享给其他任务的文件不被误删 |
| `chatgpt_result` | 增加 `delivery="reference"` 和有界 `delivery="inline"`；明确 `legacy` 默认继续保留旧客户端响应。reference 返回 session_id/result_id/分页读取信息；inline 超出 `CHATGPT_EXPORT_INLINE_MAX_BYTES` 返回 RESULT_TOO_LARGE，不静默截断 |
| `chatgpt_export_start/status/page/cancel/release` | 保留接口形状与逐 session 的快照/游标顺序。attachments 视图增补表示来源、关联与已证实的替代表示，保留原有分项 completeness。内部共享通用分页读取器，快照大内容写临时文件并保留索引；release 不销毁已物化为独立 file_ref 或被活动任务占用的文件 |
| `chatgpt_export_attachment_handle` | 作为旧下载协议适配层保留；新客户端使用下面的 file_download_start。不要让旧的一次性句柄悄然变成可重复读取 |
| 旧 `chatgpt_export` | 继续标记 legacy，不承载新大文件能力；迁移到分页和受保护文件通道 |
| `chatgpt_capabilities` | 增加 `scope="service"` 的文件传输能力发现，不启动浏览器；报告 API revision、TUS 子集、Range、临时存储、limits/TTL 与 public base 是否配置。默认 `scope="browser"` 保持原模型能力行为 |
| `chatgpt_health/browser_status` | 分别报告 Broker 资源与浏览器就绪；健康检查不再用进程活着代替可上传/可下载能力。公开返回不包含内部暂存路径 |

通用返回增加可选的 request_id、operation_id、retryable、retry_after_seconds；错误具有稳定 code、具体 stage 和恢复动作。元数据未知使用 null/unknown，不能用 0 或假定原件代替。

## 5. 新增 MCP 工具（首版七个）

以下均为拟议名称。两种传输入口各自清楚地对应用户动作，共享内部 FileStore，不做通用 action 字符串大接口。

| 拟议工具 | 核心输入 | 返回/作用 |
| --- | --- | --- |
| `chatgpt_file_upload_start` | session_id、filename、mime_type、size_bytes、sha256?、request_id? | transfer_id、upload_url、协议、有效期、限制；随后客户端通过 HTTP 发送字节 |
| `chatgpt_file_download_start` | session_id、export_id、attachment_id、request_id? | 只解析 READY manifest 的 `available/download_candidate` 项；返回 FETCHING transfer，完成后 status 带可重入 file_ref 和 Range 下载地址；不接受任意远程 URL |
| `chatgpt_transfer_status` | session_id、transfer_id | state、phase、committed_offset、total_bytes、过期时间、错误；READY 时带 file_ref 和文件描述。查询不触发重传 |
| `chatgpt_transfer_cancel` | session_id、transfer_id | 取消接收/准备操作并释放未提交数据；重复取消安全，已完成文件的删除用 file_release |
| `chatgpt_file_info` | session_id、file_ref | 可用性、字节数、SHA-256、表示来源、有效期、HTTP 下载描述、Range 能力 |
| `chatgpt_file_release` | session_id、file_ref | 释放调用方引用；活动任务/读取结束后才清理物理文件。幂等，不能因一位 agent 释放而截断另一项已获准操作 |
| `chatgpt_result_page` | session_id、result_id、cursor?、max_response_bytes? | 有界内容页、顺序/偏移、next_cursor、eof、摘要；复用 export_page 的分页引擎 |

文件描述分开表达 `representation=original|derived|crop|unknown` 和 `provenance=caller_declared|chatgpt_attachment|unknown`，包含尺寸、SHA-256、过期时间、下载 URL 与 session 头。客户端声称原件不自动升级为服务端已验证原件；派生图可正常传输使用。原文件不存在时不生成“原件”下载入口。当前 Send 文件参数接受 `{file_ref}`；`relates_to/note` 仍待任务正文关联契约和回归后开放。

上传状态为 RECEIVING → VERIFYING → READY；源文件准备为 FETCHING → VERIFYING → READY，均可进入 FAILED/CANCELLED/EXPIRED。READY 的 file_ref 可以绑定多个已授权任务，任务结束释放自己的占用，不立即销毁调用方文件引用；兼容 upload_ids 的旧路径继续按原一次任务消费规则释放。重复准备已有 READY file_ref 只返回同一资源描述，不重新下载。file_info/status 查询不突破绝对 TTL；文件失效后明确 GONE，不能静默返回另一个表示。

`files[].relates_to/note` 是可选的关联信息，便于“这份文件补充哪张图、哪个区域”。是否看不清、是否需要补充由 agent 判断，不新增自动研究、自动升档或视觉失败驱动的服务端状态机。

## 6. HTTP 文件通道与续传契约

| 拟议路由 | 语义 |
| --- | --- |
| `OPTIONS /v2/uploads` | 广告实际实现的 tus 版本、扩展和大小限制 |
| `POST /v2/uploads` | tus Creation；直接 HTTP 客户端可创建上传，与 MCP upload_start 调用同一内部服务；session 归属及文件元数据由契约明确编码 |
| `HEAD /v2/uploads/{transfer_id}` | 返回已提交 Upload-Offset、Upload-Length、有效期；不消费上传/文件 |
| `PATCH /v2/uploads/{transfer_id}` | 使用 Tus-Resumable、Upload-Offset、application/offset+octet-stream 顺序续传；偏移不符返回 409 且不写入 |
| `DELETE /v2/uploads/{transfer_id}` | tus Termination；取消未完成上传，撤销资源，后续 HEAD/PATCH 返回明确失效状态 |
| `GET /v2/files/{file_ref}/content` | READY 不可变文件的鉴权下载，可重复 GET 和单区间 Range；返回实际 Content-Type、Content-Disposition、完整文件摘要和强 ETag |
| `HEAD /v2/files/{file_ref}/content` | 文件长度、摘要、ETag、Accept-Ranges；不触发下载或消耗文件 |

旧 `POST /uploads`、`GET /exports/download/{handle}` 和 `POST /exports/{export_id}/file` 保留兼容适配，共用新存储/读取实现。旧接口的身份、一次性语义及错误格式在迁移期保持；新增路由独立使用准确的 4xx/5xx。

上传末块达到声明长度后自动进入 VERIFYING；校验通过才生成可用 file_ref，不另设必须调用的 commit。若客户端已提供 SHA-256，服务端比较完整摘要；未提供时计算并返回摘要，由客户端核对，不能称为已经与客户端原文件校验一致。可选块校验通过 tus Checksum 扩展协商，首版是否广告取决于实现和互操作验收。

每个上传资源单写者。分块先写入未提交尾部，完成必要的刷盘/校验后再推进 SQLite 提交偏移；失败回滚未提交尾部。启动时以已提交偏移对账，实际文件短于该偏移则失败，不能伪造进度；多出的尾部按恢复规则截断。最终 SHA 可流式重算，避免持久化 Python 哈希对象或将整文件读入内存。

下载 Range 返回 206 与正确 Content-Range；不可满足的范围返回 416 和总长。首版只支持一个区间，多区间可按 HTTP 语义忽略 Range 返回 200，能力说明明确这一点。If-Range 不匹配时返回完整 200，客户端必须丢弃旧拼接目标，不能把它追加到旧部分。新地址在文件 TTL 内可重入，鉴权与活动读取租约共同约束；签名源 URL 仍只在服务端内存。

完整文件 SHA-256/ETag 对应 file_ref 的同一固定表示；206 的 Content-Length 是本次区间长度，完整大小来自 Content-Range/file_info。客户端在拼接完成后核对完整摘要。不能用各次请求的 bytes_sent 累计推断文件已完整接收，因为其中可能包含重传。

## 7. 真正的大文件与流式实现

1. **上传链路**：ASGI receive → 有限块缓冲 → 暂存写入/增量校验 → READY file_ref。去掉大文件 chunks 列表和 join；浏览器端接收服务端生成的暂存路径，不再把整个文件作为 buffer 传过 Playwright。
2. **下载链路**：复用已有 browser 源解析 → 验证后的源内容流 → 限额暂存/计算摘要 → READY → ASGI 按块读取并发送。优先验证 HTTPX 对已观察内容源的流式读取；确实需要浏览器上下文的来源须独立验证有界读取/下载能力，失败时明确返回该来源不支持大文件，不能暗中退回 response.body()。
3. **首版的延迟含义**：文件先完成暂存与摘要校验，再供客户端流式下载。这保证内存有界和稳定 Range，但第一次读取需等待源文件准备完成。真正“上游边到、客户端边收”的 relay 是后续扩展，不把暂存下载冒充即时透传。
4. **上游续传**：只有观察到稳定表示、有效 Range/校验器时才续接源文件。上游不支持时，准备阶段失败只能重新获取该源；本地文件一旦 READY，客户端续传不再依赖上游。刷新过期源签名后同样需验证资产身份，不能盲接尾部。
5. **超大消息/结果**：源 API 逐页摄入不可变暂存，保留 ordinal/UTF-8 段边界与偏移索引；JSON/Markdown 输出逐项生成。只替换 ExportStore 后端而继续全量拼装 browser payload 不算完成。元数据索引可持久化，正文留在限时文件，不进入 SessionStore 或审计 SQLite。
6. **并发与背压**：块缓冲、写入队列、全局内存、暂存总量、并发流分别有上限；慢客户端不积累无限待发块。网络文件传输不占用浏览器任务槽；取源和网页附件操作仍按现有浏览器调度串行协调。

存储模式拟议为 `temporary` 与 `memory_only`。temporary 允许专用目录的限时文件和恢复元数据，支持大文件与重启续传；memory_only 保留严格小文件配额，重启后返回 GONE，不承诺恢复。当前“文件完全不落盘”的实现承诺需在采用 temporary 时明确修订；正文/文件不进入日志、凭据不落盘的边界保留。此提案尚未启用磁盘暂存。

候选起始预算（待压测冻结，不是已支持指标）：小文件内存阈值 8 MiB、顺序上传块上限 8 MiB、I/O 块 256 KiB、最多 4 个数据流、全局数据缓冲预算 64 MiB、单文件 Broker 上限 512 MiB、暂存总量 2 GiB；空闲 TTL 30 分钟、绝对 TTL 2 小时。活动读取/任务持有租约，绝对超时按明确取消策略执行。ChatGPT 上游的大小/MIME/账号限制另报 unknown/observed，Broker 配额不能冒充上游能力。

SQLite 仅持久化上传已提交偏移、文件归属/版本/摘要、状态、配额/租约和脱敏审计；复用当前数据库及维护循环，文件内容独立暂存。准备操作若需要重启后重新取源，还应保存精确 conversation URL、消息/附件 ID 等可重新解析的源身份，不能只保存会失效的 export_id，更不能保存签名 URL。无法重新核实来源时明确失败；READY 文件的读取不再依赖原快照。启动对账、取消、主动 release 和 TTL 清理使用同一资源释放路径。日志不按每个块写正文或签名 URL。

## 8. 恢复、兼容与预留

- 同一 request_id 的语义输入摘要包含 session、prompt、模型选项与文件版本/摘要及关联信息。重复相同输入返回同一操作，不同输入返回 IDEMPOTENCY_CONFLICT。完成状态必须在响应前提交，防止响应丢失导致二次生成任务。
- 网页发送与本地 SQLite 不能做原子事务。崩溃发生在“可能已发送”窗口时，使用拟议 SEND_UNCONFIRMED 并核对原会话的消息证据；无法确认前不自动重发。文件续传幂等不等于聊天发送恰好一次。
- 已准备文件可在 TTL 内复用 file_ref，补充到同一 session 不必下载再上传一份相同字节。远程客户端自有原文件仍经上传通道导入；同 SHA 重复补图不被宣称更清晰。
- FileStore 先做内存/临时文件两种实现，共用逐块写、read_at、stat、pin/release。预留对象存储适配点，但首版不引入对象存储、分布式队列或全局内容去重。
- 能力发现预留 relay、并行上传、未知总长、事件推送和小图预览；未实现均返回 false/unsupported，不输出假入口。未知必需能力明确失败，不能静默换原件、降模型或改 session。
- 标准 MCP 进度通知可作为优化，status 始终是恢复入口；不依赖 SSE 长连接来保留文件状态，也不把事件重连当作字节续传。
- 三种流分别声明能力：文件字节流、任务进度通知、生成中回答预览。首版实现文件流并保留 status；回答预览可后续增加带 revision 的增量/替换事件，须先确认实际页面事件来源，不承诺逐 token 或只追加。预览不充当冻结 result_id 的最终正文，断线后仍能读取完成结果。
- 公网 URL 由受信的部署配置生成，兼容现有 neko-dashboard.com:32751 映射；不根据任意 Host/Forwarded 头拼接。MCP 与文件路由同源、同认证；具体 HTTP/HTTPS 配置和代理方法支持由 G4 实测，不在设计中臆定。
- 客户端辅助层封装 tus、Range、摘要校验和本地图片输入，把文件字节留在工具执行环境，不逐块占用模型上下文。仅支持 MCP 而不能访问 HTTP 或图片输入的客户端必须报告能力缺口；返回 resource link 不等于模型已读文件。
- 接收端校验结果保存在客户端传输回执/验收报告；服务端审计区分源文件已准备、已发送字节与任务页面附件已验证，不把 HTTP 正常结束写成客户端已校验，更不写成 agent 已识别。首版不为了这个区分另建视觉判定接口。
- 旧 inline 风险在兼容窗口仍存在。只有迁移清单中的客户端均显式使用引用/分页并通过验收，才能改变旧默认或移除兼容入口；此前不得宣布所有调用方式均无截断。

## 9. 落地顺序与退出门槛

| 阶段 | 实施结果 | 验收/纠偏门槛 |
| --- | --- | --- |
| I0 契约及复用验证 | **通过**：固定 MCP/tus 版本并检视 3 个 ASGI 候选；Windows/许可/生命周期证据支撑选择最小标准子集。 | 独立 tus-js-client 4.3.1 完成真实 HTTP 上传并用 HEAD 校验长度、摘要和 file_ref；不把它当作上游 ChatGPT 的 Range 证据。 |
| I1 单 Broker 与 FileStore | **代码实现，本地测试通过**：SQLite 记录上传偏移、摘要、session、任务 pins、过期和引用；大文件 Path 交给 Playwright。MCP 新文件工具的 stdio 调用代理到 HTTP Broker。 | 多 FileStore 实例重开后续传、wrong-session、TTL/清理、task pins、task request hash、两个真实进程争抢同一 Upload-Offset 与注入写失败后的 offset 回滚均有回归；真实 OS 磁盘满和多 Broker 争用未测。 |
| I2 标准文件通道 | **代码实现，本地测试通过**：tus Creation/HEAD/PATCH/Termination、session 头、偏移和 SHA-256、可重复 GET/HEAD/Range/ETag；旧路由保留。 | 标准 JS tus 客户端、断点恢复、Range 合并、206/416、摘要校验通过。TUS Checksum 扩展没有广告；真实 FRP/公网代理未验证。 |
| I3 任务/结果/浏览器集成 | **页级 spool 已实现；压力门槛部分通过**：源 API 页按游标逐页规范化到临时 JSONL spool，旧页顺序、ID/hash 去重和冲突状态保留；附件引用多次扫描 spool，不再保留整段消息数组；ExportStore 三视图磁盘化，归档按块发送。 | 同一目标会话 2 API 页、65 raw/63 visible，106 消息页重组 SHA 通过；22 个 available/candidate 附件已有同版 22/22 长度/MIME/SHA 真实物化证据，3 个 Library 原件 unavailable。当前 126 tests passed/1 opt-in skipped；256 MiB 流、慢读、写失败 offset 回滚和多进程 offset 争用已通过。仍需单页/单超长消息峰值、组合磁盘高水位及真实 OS 磁盘满压力。 |
| I4 客户端与发布 | 未进入：生产 Broker 仍未重启；公网/FRP、真实 agent 识别和回滚未验证。 | 先完成 I3 压力门槛，再通过 cc-switch/真实 MCP 客户端验证无截断、图片识别和同会话按需往返；之后按 G4 验证公网及回滚。skill/cc-switch 同步由用户确认，WebDAV 同步仍由用户验证。 |

每阶段入口回顾相关 BC、参考与未证假设；出口记录源证据、失败集合和是否需要改方案。若源适配器无法有界读写，先收窄能力或更换有证据的适配器，不继续堆重试/缓存层。

验证矩阵包括：0 字节/普通文件/上限附近/超限，客户端断线与响应丢失，服务重启及未提交尾部，磁盘不足/TTL/取消，错误偏移与并发 PATCH，错误 SHA/源内容变更，HEAD/206/416/If-Range 返回 200，跨 session/过期引用，slow reader 背压，长结果分段及 export 重读。大文件至少用 256 MiB 和候选上限样例衡量整个 Broker/驱动链路；实际 ChatGPT 仅测试其已确认支持的大小，拒绝不支持的文件也须正确反馈。

当前本地验收目标为 I0–I3 的已实现部分：全量 Python 回归与独立 tus 客户端互操作；256 MiB 文件流、目标真实消息页 spool/分页及 22 项附件传输均有证据，3 个 Library 来源仍不可用。未重启生产 8787、未修改 FRP/公网服务。

### I3 快照磁盘后端入口复盘（2026-09-26）

- **证据/问题**：`ExportSnapshot.views` 保存 messages/markdown/attachments 三份完整 `bytes`；消息页另保存每条序列化副本，状态/分页/HTTP 文件下载都直接读取这些 bytes。当前容量是聚合 64 MiB，且 HTTP 归档仍返回完整 `bytes` 后再切块发送。
- **薄适配选择**：沿用现有单 Broker `jobs_dir` 文件系统和 stdlib 文件 I/O，为快照写 per-export 临时目录、长度/SHA 与消息偏移索引；分页只 seek/read 所需区间，HTTP 文件路由直接按固定块发送。无需新增 SQLite 数据库、对象存储、任务队列或重复一套文件协议。
- **为何不直接复用 FileStore**：FileStore 是 session file_ref 的上传/下载资源，具有 task pin、transfer 状态和可公开下载身份；把内部三视图伪装成用户文件会扩大 API 语义并增加引用生命周期。快照继续由现有 ExportStore/export_id 绑定和清理，只复用本地磁盘与分块 I/O 模式。
- **失败/清理**：写入使用同目录 staging，只有三个视图、哈希和索引齐全后才发布 READY；写失败删除 staging，不影响同 session 其他快照。release、session close、idle/absolute TTL 删除该快照目录；服务重启后持久 READY 仍按现有契约置 GONE，孤儿目录清理按独立检查实现并验证。
- **边界/验收**：暂时维持显式 64 MiB snapshot quota；browser 仍先在内存聚合全部 API pages，所以本阶段只能消除快照副本、分页和归档读回的无界内存，不能宣称源摄入端到端有界。验证目标是磁盘内容重组与旧快照完全一致、游标重试/UTF-8 分段/SHA 一致、跨 session 隔离、TTL/release 清理、HTTP 分块慢读及旧客户端兼容。源 API 逐页落盘另列后续门槛。

### I3 源页 Spool 出口复盘与压力入口（2026-09-26）

- **出口实证**：源页经目标 URL/游标校验后逐页归一化并写入临时 JSONL spool；Older 页按源序反向索引后输出，ID/hash 去重并将冲突标 partial。附件引用对可重迭代 spool 扫描；ExportStore 的三视图仍写入 Manager 私有临时目录，HTTP 归档按块发送。spool close、TTL/release/session close 与进程退出清理临时正文；READY 快照重启后仍为 GONE。
- **验证**：全量 pytest **126 passed, 1 opt-in skipped**；新增 spool 顺序/重复/冲突/容量、make_export 等价、JobManager 消费/释放、写失败 offset 回滚、双进程 offset 争用和孤儿 spool 清理回归。单独运行的 256 MiB TUS + 慢速完整下载通过，长度/SHA 与 256 KiB 最大回包块匹配。真实 ChatGPT `stream_pages=True` smoke：2 API pages、65 raw/63 visible；messages 106 页重组 SHA 通过；25 项附件、22 available/candidate、3 Library unavailable；spool 820,149 bytes，消息 verified、附件 partial。此前同版下载器对 22 项实际逐项长度/MIME/SHA 通过，共 7,270,631 bytes。生产 Broker 未重启。
- **复盘**：HTTPX 直接请求最终内容端点因没有登录 cookie 返回 403；页面和 cookie jar 请求对 HTML 文件返回 200 `text/html`，且 Content-Disposition/真实文件字节验证一致。当前只把 Playwright 针对初始 URL 匹配的 cookie 放入 HTTPX 域/路径受限 jar；跨域重定向不会携带 ChatGPT cookie。短期签名 URL 和 cookie 均只在进程内。
- **边界**：`measure_page` 在 Browser 对单页 JSON 解码之后、归一化之前检查累计 64 MiB source-page budget；SDK 已解码的一个超大响应页仍会瞬时驻内存，归一化页也与它短暂共存。单条超长消息的 Markdown 仍按消息而非字符格式化；不宣称零内存或任意大小导出。
- **剩余压力门槛**：测单个超大 source page/message 峰值、spool+retained snapshot 的组合磁盘高水位和真实 OS 磁盘耗尽；256 MiB 文件流、慢读、多进程 writer、注入写失败回滚、TTL/取消基础回归与孤儿 spool 清理已有证据。之后进入真实 cc-switch/客户端、纯净 Luna 与公网/回滚验收。
