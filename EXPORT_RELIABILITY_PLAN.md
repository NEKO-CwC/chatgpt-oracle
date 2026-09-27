# 会话导出完整性与交付改造计划（2026-09-24 纠偏）

> 状态（2026-09-26）：当前候选最终全量 `pytest -q` 为 126 passed、3 skipped；两个新增 opt-in 大消息压力测试与此前 256 MiB TUS + 慢读完整 GET 分别单独通过。合成 16 MiB 单消息从源页规范化、spool、快照到消息分页 EOF 完整重组；spool+快照并存磁盘高水位 83,887,160 bytes，tracemalloc peak 134,262,436 bytes。24 MiB 单消息超过默认 64 MiB 快照视图总配额时显式失败且不发布部分快照；真实磁盘耗尽未测（不填满工作站磁盘），写入错误注入已有回归。目标会话真实 `stream_pages=True` 读取为 2 API 页、65 raw/63 visible；messages 106 页重组 SHA 通过，25 项附件中 22 项有来源/候选、3 个 Library 原件仍无来源，附件为 partial。22 个可用/候选文件已有同版真实物化校验（7,270,631 bytes，长度/MIME/SHA），G2-byte 仍 partial。纯净 agent、真实客户端、生产 Broker、公网/FRP 和回滚仍未验收，8787 未重启。

## 目标与边界

- 对指定 conversation URL 按源站游标直接读取消息，保留源顺序、消息类型和分支元数据；只有拿到可信页边界且页链一致，才声称消息历史完整。
- 区分模型阅读和用户归档：MCP 分页交付可直接阅读的完整消息对象；JSON/Markdown 归档走受保护的文件下载。两种交付使用同一不可变 export_id 快照。
- 附件发现、附件覆盖和附件字节下载分别取证。正文提到 HTML/ZIP 不证明文件曾附在会话；看不到入口时报告“未观察到”，不伪造或猜测地址。
- SessionStore 只保存会话 URL、任务引用和 TTL 元数据。导出正文及短期文件句柄不写入 SessionStore、调度 SQLite 或审计日志；凭据、签名 URL 和完整对话正文不进入日志或客户端元数据。
- READY 只表示快照可读。messages 与 attachments 分别标记 verified、unverified 或 partial；overall 只有两者均 verified 才为 verified。目标会话若没有相应终点证据，就不能宣布“完整导出”。
- **用户验收澄清（2026-09-26）**：当前业务目标是让 agent 看懂会话图片，不要求复刻 OpenAI 的原始存储字节。视觉表示与字节归档分开验收；pointer PNG 可以满足 agent 输入目标，但必须如实标为派生表示，不能把 Library 原件标成已下载。原始字节完整性不是当前视觉识别目标的发布阻塞，除非用户以后重新要求归档保真。
- **按需补充文件（2026-09-26）**：用户要求在 skill 中提供原文件下载/回传的选项。下文新增该协作设计，复用已有导出下载、上传和同 session 续问接口；是否补充文件由当前任务的识别缺口决定。手册更新与真实往返验收分别记录。
- **大文件与接口扩展（2026-09-26，设计提案）**：全接口兼容策略、七个拟议新工具、tus/Range 文件通道、统一 Broker、临时文件及重启恢复见 [MCP_INTERFACE_PLAN.md](MCP_INTERFACE_PLAN.md)。新增能力尚未实现；该子计划的 I0→I4 承接下文协作流程，文件传输通过后仍需真实 agent 识别及 G4 验收。

## 已有证据与候选状态

| 来源与时间 | 已观察到的事实 | 可得出的结论 |
| --- | --- | --- |
| 2026-09-23 旧导出缓存 | exp_44be2a196cfb41ae961c 返回 6 条消息、2 个附件、约 213 KiB 响应；当时任务为 COMPLETE。 | COMPLETE 仅表示任务结束；没有源页、消息 ID 或传输原始响应证据，不能证明会话完整。 |
| 2026-09-24 较早的 DOM 复查 | 同一 URL 读取 8 条消息、2 个附件，来源为 DOM，无历史终点。 | 与旧缓存不是同一快照，不能由 6 与 8 的差异推断丢失原因。 |
| 2026-09-24 前次真实页面网络观察 | 页面请求取得最新页 51 条，随后以 start_cursor 为 before 请求取得较早页 14 条；较早页 has_previous_page=false。两页共 65 个原始消息 ID，未见重复；另观察到 6 个 API 附件引用及 5 个 interpreter 文件元数据（2 PNG、3 HTML）。 | 直接游标接口和较早历史终点有实测依据。65 是当次原始节点数，含 tool/system；仍须在同一次新读取中验证最新边界、页链、分支和附件覆盖。 |
| 手工请求与当前候选读取 | 未复用页面请求上下文的手工 fetch 曾得 404/401；页面自身请求成功。当前 browser.py 监听 API 响应，却仍用滚轮触发相邻页；一次候选读取报告 63 个非 system 节点和 11 个附件记录。 | 404/401 不能证明页面 API 不可用；滚轮读取与消息/附件数均不能作为本轮验收结果。需检查页面 JS 中的请求构造和客户端上下文，不能输出鉴权值。 |
| 2026-09-24 阶段 0 页面 JS 与直接请求复现 | 当前目标页的客户端 chunk 中，v6t 调用 safeGet GET /conversations/{conversation_id}/messages，query 包含 before、include_has_versions、num_turns；p6t 以 has_previous_page ? start_cursor : null 取下一旧页游标，外层循环检测游标不前进。请求使用 SendIfAvailable 和页面请求上下文。一次不滚动直请求返回最新 51 条（has_previous_page=true、has_next_page=false），以同页 start_cursor 请求旧页返回 14 条（has_previous_page=false），无重叠，65 个唯一 ID。仅记录路由、标志、计数及请求头名称，未记录正文、游标值、请求头值或签名 URL。 | G0 的源端 API/游标证据已通过；两端页界和附件覆盖仍按后续阶段单独验收。此 65 条是这次读取的证据，不是永久总数。 |
| 2026-09-24 附件响应链复查 | interpreter/download 返回 application/json，含文件元数据和短期 download_url；同次页面另请求 estuary/content，响应为 PNG/HTML。 | interpreter 元数据 URL 不是文件字节 URL；现有附件句柄下载链需在阶段 2 修正。签名参数未输出或保存。 |
| 会话中断前的附件传输复核 | 同一指定 URL 的一次候选运行取得 10 项：3 个 asset pointer PNG 与 4 个 interpreter 文件经句柄下载并通过长度/MIME/SHA，3 个 library_file_id unavailable；另一次候选运行只得到 6 个 unavailable 项。 | 曾成功的 7 个传输证明来源路径可用，但不同快照不能证明 inventory 恒定。当前回归必须查清可观测请求差异并可重复传输；3 个 library 引用仍不因相似 ID 获得猜测路由。 |
| 2026-09-25 本地候选 stdio E2E | 同次读取 messages=verified（63 条，105 页重组，820,571 bytes，SHA 匹配）；附件 6 项全 unavailable，0 个句柄传输；SQLite `SOURCE_ATTEMPTS` 只有 conversation 与 conversation_messages，无 interpreter/file-download 请求。 | MCP 消息传输闭环通过，G2 失败。现有 `file_download_response_urls` 未用，导出发现仅依赖响应事件；Performance API 资源回补尚未实现。缓存是可能原因而非已证事实，须以精确资源条目核实。 |
| 2026-09-25 E2E 验收边界纠偏 | 后续核对 `server.py` 发现 stdio 的 export tools 会代理到 `127.0.0.1:{settings.port}/internal/exports/*`；因此通过本机 stdio 执行的两轮 E2E 实际由运行中的 8787 Broker 完成，不能证明其间本地 browser/jobs 候选有效。 | 两轮计数保留为“运行中 Broker 观测”，不作为候选补丁验收。候选回归须在临时端口启动当前代码的 Streamable HTTP app，使用独立临时 jobs/SQLite/Session 目录，再运行既有 `scripts/export-e2e.py --transport http`；不重启 8787。 |
| 2026-09-25 候选 UI 覆盖复查 | 真正隔离候选 HTTP E2E 取得 63 条 verified 消息、9 个附件项：3 pointer PNG、3 interpreter 输出、3 unspecified/unavailable；6 文件 1,021,258 bytes 均通过长度/MIME/SHA，G2 仍 partial。扫描稳定结束于 11/63 个唯一 DOM ID；同轮却观察到 1 个 UI 旧页响应、14 个消息节点。 | UI 旧页有响应但当前扫描只遍历一次，存在页面状态异步提交后未重新枚举的可能；该因果仍是假设。候选需在旧页响应 quiet 后重扫，并证明唯一 DOM ID 增长/覆盖；不改变 direct API 消息快照来源。 |
| 2026-09-25 早期隔离候选 HTTP E2E（direct-reference 重构前的历史基线） | 临时 8789 候选 app + 独立 SQLite，messages=verified：2 个源页、65 raw/63 visible；MCP 重组 105 页、820,775 bytes、SHA 一致，最大 HTTP page 15,446 bytes。附件 9 项：3 image_asset_pointer PNG、3 interpreter_output、3 api_attachment；6 个 available 文件共 1,021,258 bytes，逐项 Content-Length/MIME/SHA 匹配，3 个 api_attachment 无 source；附件和 overall=partial。 | 该结果只作为旧候选基线，不代表当前 inventory。后续 direct-reference 候选由“当前目标会话复核”记录；8787 未重启，旧候选未部署。 |
| 2026-09-25 login_required 误判，已纠正 | 一次新隔离页读取因 3 秒内没检测到 textarea/contenteditable 而被错误映射为 login_required；随后现有页 browser_status 为 ready/logged_in，且同 Profile 新隔离页 direct export 成功读取 63 条消息，没有认证 URL 证据。 | login_required 仅由明确 auth URL/可见登录表单触发；成功的 latest conversation API response 也证明认证有效。普通 composer/hydration timeout 改报页面未就绪，不要求用户重登录。 |

现有代码已包括 export_start/status/page/attachment_handle/release、TTL 磁盘快照、stdio 到本机 HTTP Broker 的转发、SQLite 导出事件和轮转日志。当前本地全量 pytest 为 118 passed；目标真实会话页链 verified、22 个来源附件传输校验通过，3 个 Library 来源仍 unavailable。生产 Broker、公网及 FRP 路径未验收。每阶段分别记录“代码已改、测试通过、真实源站通过、客户端通过、已部署”，不合并状态。

## 参考与复用决策（阶段 0 硬门槛）

指定参考：[ChatGPT Exporter - ChatGPT to PDF, MD, and more](https://chromewebstore.google.com/detail/chatgpt-exporter-chatgpt/ilmdofdhpnhffldihboadndccenlnfll?hl=en)。目前仅核查过其公开说明，能参考导出范围、富内容及文件交付行为；未取得该扩展的可核实源码或目标会话的导出样本，不能推断它使用了哪条分页 API。已有 UPLOAD_REUSE_RESEARCH.md 讨论的是上传，不充当导出参考。

| 已核查来源 | 源码证据与可借鉴范围 | 不能据此声称 |
| --- | --- | --- |
| [imaustink/chatgpt-exporter，固定版本](https://github.com/imaustink/chatgpt-exporter/blob/adec14341eb36121e0dc5b066d99e1eb0501df39/export.ts#L38) | Playwright 在已登录页面内请求会话 API，并分别处理鉴权与重试；MIT。优先借鉴与现有 Playwright 栈相容的请求上下文和错误处理。 | 它的单会话读取使用 conversation/{id}，不能证明当前逐消息 before 游标或附件覆盖。 |
| [k8se10/chatgpt-archiver，固定版本](https://github.com/k8se10/chatgpt-archiver/blob/2cee5bd7828995f4d69ac1ec86dd4157bdec4bf1/chatgpt_archiver/api.py#L74) | 在页面内发认证请求，以 HTTP 状态区分 401/403、429；[文件代码](https://github.com/k8se10/chatgpt-archiver/blob/2cee5bd7828995f4d69ac1ec86dd4157bdec4bf1/chatgpt_archiver/api.py#L229)展示针对 `image_asset_pointer` 先取短期文件 URL 再取内容。可借鉴状态处理与按需获取文件。 | 使用 Selenium 且单会话接口形状不同；代码没有证明 `library_file_id` 或普通 API attachment 的下载路径；仓库许可证元数据未明确，确认许可前不复制代码。 |
| [pionxzh/chatgpt-exporter，固定版本](https://github.com/pionxzh/chatgpt-exporter/blob/a17262cc3ff7c31660dd7239184d8fcdcc105401/src/api.ts#L434) | MIT 导出器提供会话 mapping，并在 `fetchImageFromPointer` 中处理 `sediment://` image pointer 的文件 metadata→content 两步链路，可对照内容模型与重试。 | 代码没有建立 `library_file_id` 下载路径；其 conversation/{id} 接口不能替代本会话真实分页证据，也不能把 pointer 路由套用到其他引用类型。 |
| 目标 ChatGPT 页面的前次网络观察 | conversations/{id} 最新页和 messages?before=... 较早页共 65 个原始 ID，较早页 has_previous_page=false；这是本项目逐消息分页的直接线索。 | 仍未核查页面 JS 的请求构造及同一次读取的最新边界，不足以冻结适配器。 |

阶段 0 必须产出带版本/路径证据的对照矩阵：逐项列出认证上下文、单会话分页、顺序/分支、附件发现与下载、输出交付、错误重试和许可证；标注“可直接复用现有库/代码模式”“只需薄适配”“确需新实现”。同会话的指定扩展样本可得时必须对比消息、附件和格式；拿不到样本须记录原因与替代证据。页面 JS/真实请求的逐消息游标证据和至少一个可核查开源实现的代码对照是进入阶段 1 的必要条件。若参考只覆盖旧接口，先完成源端实证，不能把旧形状硬套进新代码。没有可核查证据就停留在研究阶段，更新阻碍与下一验证动作。

## Badcase 登记与回归

“已观察”只描述发生过的现象；“风险待验证”不能写成已证实故障。每次复现失败、参考与实测冲突或阶段验收失败，先在此表增列证据、影响、修正和回归，再修实现。

| ID / 状态 | 已有证据或风险 | 纠偏与必须通过的回归 |
| --- | --- | --- |
| BC-01 已观察，截断层未知 | 旧导出仅报告 6 条/2 附件，约 213 KiB；客户端显示截断，缺原始 MCP 响应。 | 同一次导出对比源消息 ID、服务端快照、原始 MCP 响应和客户端重组摘要；不把任务 COMPLETE 当内容完整。 |
| BC-02 已观察，不能跨快照归因 | 同一 URL 曾有旧缓存 6 条、DOM 8 条、API 两页 65 个原始节点，采集时间和表示不同。 | 每次验收保存本次页边界、原始/可见计数和消息 ID 摘要；历史数字只作诊断。 |
| BC-03 历史问题已复现并在候选修复 | 页面 JS 使用 safeGet/SendIfAvailable；真实请求带 authorization、oai-session 与 x-openai 路由/客户端头。复用本次动态头及 messages target headers 的不滚动请求返回 200。 | 只输出头名称和状态；401/403、429、响应形状分别处理，聚焦测试与真实请求均通过。 |
| BC-04 历史实现问题，候选已移除 API 滚轮 | 原 browser.py 用多次 mouse.wheel 触发 API 下一页，造成等待和页面跳动。当前 api_paged 路径改为直接 before 请求；DOM 回退仍单独标记。 | 对目标会话执行 api_paged，2 页/65 原始节点/终点 verified；正常 API 分支没有 wheel 调用，DOM fallback 不宣称完整。 |
| BC-05 原路径已修正，类别覆盖待验收 | interpreter 元数据 URL 被保存为来源；候选现按稳定标识重取 JSON 并立即读 estuary 内容。真实 PNG（143,534 bytes）及 HTML（33,466 bytes）下载、MIME 与 SHA-256 均通过，文件未落盘。 | 对 ZIP 等剩余类别继续逐类型核验；签名 URL 仅在请求内存中使用，不把元数据 JSON 当文件内容。 |
| BC-06 原逻辑耦合已修正，独立附件验证待做 | 旧逻辑沿用消息完整性；当前候选把附件独立标为 unverified，元数据失败时标 partial。 | 附件阶段构造消息 verified、附件 partial/unverified 的用例，并验证真实附件库存和内容。 |
| BC-07 已确认计划问题 | 此前只有扩展营销资料和“参考样本可选”，没有代码级复用决策、badcase 登记或逐阶段复盘门槛。 | 阶段 0 参考矩阵及以下入口/出口复盘记录齐全后才启动下一阶段；缺项时保持未通过。 |
| BC-08 本地候选已修正，生产待验收 | SQLite 导出事件原无明确保留清理；进程内快照 TTL 原依赖访问时清理；原运行中导出只在 browser.export_conversation 返回后检查取消。上一候选曾将分页请求硬限 4,096 次。 | 保留 90 天 request/event 裁剪、无访问 TTL GONE、8 项等待队列和 cancel/slot 确认；移除 READY 快照分页拒绝，调用只更新持久聚合计数，view EOF 才落审计事件。全量本地测试通过，生产 Broker 待验收。 |
| BC-09 原 byte-chunk 方案已替换，MCP wire 回归通过 | MCP 返回 byte chunk 要求客户端手工重组。MCP 1.30 普通 JSON 文本结果会二次编码；当前 page tool 改用空 content 的 structuredContent，并把 tool-result JSON 限为 15 KiB。 | MCP 1.30 回归以 response.aread() 实测 Streamable HTTP wire；转义密集长消息实际 E2E 需保持在 16 KiB 内。 |
| BC-10 已观察，不能按时间戳排序 | 最新候选的 63 条可见消息中 create_time 非单调；规范化输出顺序与两页源数组按可见 role 过滤后的 63 个 ID 完全一致，65 个原始 ID 唯一。 | 以 API 页数组顺序为准并按页链拼接；乱序时间戳夹具仍须保留源 ID 顺序，禁止按 create_time 重排。 |
| BC-11 file_id 不足以证明可下载，候选已纠偏 | 一个 API 图片引用按源码 route/query 直取返回 file_not_found；隔离页稳定渲染后也未找到对应可见卡片。979 消息控制读取中的 ZIP 引用也没有可用来源；interpreter PNG/HTML 则通过两步刷新成功。 | 不由 file_id 单独推断 downloadable；未知来源保留记录但不生成句柄。仅对可验证来源或明确报告 downloadable 的 React card 提供句柄；附件整体保持 unverified。 |
| BC-12 HTTP E2E harness 参数错位，候选已修复 | MCP 1.30 的 Streamable HTTP transport 返回 read/write streams 加 session-id callback；ClientSession 只接收前两项。流式响应没有 Content-Length。 | E2E 只传 read/write streams；用 response.aread() 累计实际 wire bytes；内存 ASGI start/status/pages/release 和摘要校验通过。 |
| BC-13 已观察并修正 release/cancel 混用 | 原 `chatgpt_export_release` 对 QUEUED/RUNNING 实际发取消请求，清理 READY 快照与停止浏览器读取共用一个操作；调用方无法单独表达取消，也可能误以为已释放。 | 增加独立 `chatgpt_export_cancel`；release 只接受 READY（终态幂等返回），活动导出返回 `EXPORT_NOT_READY` 并要求调用 cancel。运行取消等待子任务确认，READY 清理不碰队列/slot；覆盖运行中 release 不取消、cancel 后槽位清空与 session close 回归。 |
| BC-14 已观察并修正重启队列未被 status 触发 | 持久队列重启后可正确保留 QUEUED 和空闲 slot，但 MCP/HTTP 的 export status 分发此前没有启动 Manager worker；客户端只轮询状态会一直看到 QUEUED。 | 在统一 MCP/HTTP export dispatch 上幂等启动 Manager，启动时把进程内 READY 快照缺失项审计为 GONE，并恢复可消费队列；测试仅从 status 请求触发恢复。 |
| BC-15 源站页数曾错误复用 count 上限，已本地修正 | `browser.export_conversation` 原用 `range(export_max_count)` 控制旧页请求；该值默认 1,000，也被用于用户的 `count` 参数验证，1,000 页后可能未到源站最早页就停止。 | 已分离两者：源页循环由 `has_previous_page=false` 结束；缺失/重复 cursor、URL 偏移或请求失败标 partial。合成 1,002 页回归通过。真实目标长会话仍待验证。 |
| BC-16 客户端读取曾有 4,096 次硬上限，已本地移除 | 1 KiB MCP 页和大快照可能需要超过 4,096 次，导致 READY 快照已生成却无法读到 EOF；逐页 SQLite event 还会造成日志表按每次读取增长。 | 已移除拒绝阈值；`export_jobs.page_requests` 聚合持久计数，非 EOF 页不逐条插入 event，view EOF 记录计数/摘要。跨过 4,096 后读到 EOF 的回归通过；TTL 和快照容量仍是明确资源政策。 |
| BC-17 附件字节之前无客户端可核验摘要，已本地补齐 | attachment handle 让 Playwright 将响应 body 读入内存，ASGI 再分 64 KiB 写出；原客户端 E2E 只检查 manifest。 | 下载响应增加 Content-Length 与 X-Content-SHA256，SQLite 记录服务端实际字节数/摘要；E2E 收集器逐个取回 available 项，核对长度、MIME、manifest size 和 SHA-256，不落本地文件。Fake HTTP 回归通过；真实源站附件和公网下载仍待验收。 |
| BC-18 客户端向浏览器上传路径已存在，本地组合回归通过 | HTTP `POST /uploads` 流式收字节，返回短期 upload_id；`submit/continue/resume(upload_ids=...)` 经 UploadStore 转交 Playwright filechooser/input。当前有 25 MiB/文件、64 MiB 总内存、8 个文件限制。 | 本地组合测试覆盖 submit/continue/resume 的原字节传递与句柄清理；独立 Chromium 回归验证 filechooser 和 input 两路的字节、MIME、文件名和 SHA 一致。真实 ChatGPT 上传和生产 Broker 仍待验收。README/Skill 已列出 resume 上传入口。 |
| BC-19 文件卡片下载曾复用现有会话标签页，已本地改为隔离页 | `/backend-api/files/download` 路径原用 `_conversation_page` 复用同 URL 标签页，再滚动历史寻找虚拟化卡片；可能扰动用户正在看的 tab。 | 在单独后台临时页加载精确会话，仅为卡片动作滚动并于下载后关闭；正文 API 分页仍不滚动。Fake page 回归确认原 tab 未导航/关闭；真实文件卡片路径待 E2E。 |
| BC-20 指定会话附件旧快照不完整，历史记录 | 2026-09-24 某轮读取为 6 个 PNG reference unavailable、2 个 interpreter HTML 有来源，DOM 8/63。之后候选与源端快照有变化。 | 保留为历史 badcase，不跨快照推断缺失类别；当前状态以同一次隔离候选 E2E 为准。 |
| BC-21 附件失败异常曾写入内部 file_id，已本地脱敏 | ASGI 下载失败用 `logger.exception` 输出浏览器异常文本，其中包括内部 `file_id`。 | 改为只记录 `error_type`，客户端仍收到通用 `DOWNLOAD_ERROR`；真实服务日志扫描待本地测试确认。 |
| BC-22 虚拟化附件扫描的稳定条件不等于覆盖，已由 API 引用清单替代成功路径扫描 | 历史 `_load_all_rendered_messages` 在 63 条消息中只渲染 8–14 条，最长等待达 120 秒，无法证明附件 inventory 完整。当前 API 成功分支不再调用整页滚动；DOM 加载仅留给 API 失败的 partial fallback。 | 当前正常路径从完整 API message pages 枚举 attachments、asset pointers 和 assistant sandbox links；manifest 状态取决于引用对账和逐项 metadata，不以 DOM 覆盖数作为完整性证明。fallback 仍不宣称完整。 |
| BC-24 附件来源数量曾随页面状态变化，已改由 API 引用清单审计 | 同一目标的 API 页链完整读取 65 raw/63 visible；源 API 中 6 个 attachment records、3 个 image pointers，assistant 正文还有 16 个不同 sandbox file links。16 项 interpreter metadata 均可从 message_id+sandbox_path 直读，不依赖 UI 渲染数。 | 完整性对账基于同一 export 的 API references：普通 attachments、library refs、rich-part pointers、sandbox links 分类别计数；跨快照的 response/Performance 数量不相加。 |
| BC-25 stdio 候选验收误路由，已纠正 | `server.py` 明确表明 stdio export proxy 调用配置的 HTTP Broker；前两轮脚本指向运行中 8787，执行的不是当前候选 worker。 | 不用 stdio E2E 评判未部署候选。改用临时端口 + 候选 app + 临时 SQLite 跑同一 HTTP E2E；候选证据、运行服务证据和公网部署证据分开记录。 |
| BC-26 UI 旧页静默后重扫仍不足，正常 API 成功路径已停止滚动 | 历史扫描两轮仅覆盖 14/63 IDs；针对 library refs 未渲染出 React card。当前直接 API 引用路径成功发现 16 个 sandbox outputs、3 个 pointers 和 6 个 API attachment records。 | 不再通过增加滚动或扫描时间来补 inventory。仅 API 失败 fallback 和明确的单卡动作可以触碰 UI；其结果仍需逐引用校验并标注来源。 |
| BC-27 普通 API attachment 无来源，已在同源请求上下文验证 | 3 个 tool-message attachment 的 API `id/name/mime_type/size` 无 URL。精确观察到的 `/backend-api/files/download/{id}?conversation_id=...&inline=true` 返回 metadata；signed content query ID、文件名、大小和实际 PNG MIME/字节均匹配。 | 当前候选直接解析这 3 项，客户端 3 项均已通过传输校验。library refs 是不同类别，不因共享同一消息 ID 或 pointer alias 自动继承来源。 |

| BC-28 composer readiness 被误当成登录态，已本地修正 | composer 在新隔离页 hydration 慢时不可见；旧逻辑固定等待 3 秒后直接抛 login_required，与认证状态无关。现有 Profile 实测 browser_status 为 ready/logged_in，修正后 direct export 实际读取成功。 | 显式 auth URL/可见邮箱登录表单才请求用户登录；成功的 latest conversation API response 作为认证证据；其他 readiness timeout 返回页面未就绪。覆盖明确 auth URL、正常 conversation URL、authenticated response 的单测通过。 |
| BC-29 attachment/pointer 同键导致身份混并，已本地修正 | 三个 user attachment records 的 `id` 与同一 message 的 asset pointer ID 相同，但 `library_file_id` 不同；旧 dedupe key 只含 `(message_id, attachment_id)`，会将 pointer source 套到 library 原件。 | 以 file kind 和真实 file/library ID 组成 identity；library handles 改用 library ID。回归确认 library unavailable 项与 pointer item 独立，下载响应按命中的具体 ID 字段关联。 |
| BC-30 interpreter 文件清单曾依赖虚拟 DOM，已改为 API 正文链接 | 完整 API messages 中发现 16 个 assistant `sandbox:/mnt/data/...` 链接：3 HTML、2 ZIP、2 JSON、2 Markdown、7 PNG。逐条 interpreter metadata 200、名称/MIME匹配；浏览器内流式读取 16/16，5,850,463 bytes，无落盘。 | 成功 API 导出解析 message text 中的 sandbox refs 并按已观察 `message_id+sandbox_path` 路由取 metadata；短期 URL 只在内存中使用。MCP E2E 同轮 16 项均通过 HTTP 长度/MIME/SHA。 |
| BC-31 library 原件与 pointer derivative 混淆，G2 仍阻塞 | 目标 user message 的三条 attachment metadata 分别带 `id=file_…` 与不同的 `library_file_id=libfile_…`，声明大小为 512,134/494,544/357,736。资料库三项仍显示 available；实际 Library `downloadFile` 动作、独立 `GET /api/library/files/{library_file_id}/download`、带正确会话上下文的 `/backend-api/files/download/{file_id}` metadata→content 路径，以及 inline pointer 对应字节均为 310,807/311,171/183,729-byte PNG，且 SHA 与 inline pointer 逐项相同。独立 Library GET 三项均为 302，Location host/path 指向 `chatgpt.com/backend-api/estuary/content`，最终 200 `image/png`；无 Content-Length，Content-Disposition 是原文件名。第一项的客户端 `downloadIntent=true` 分支也得到相同派生图。 | 精确 Library native route 已验证但当前三个对象仍 resolve 到派生图；“文件仍在资料库”不等于原始上传字节可下载，也不能把它描述成文件已删除。G2 继续阻塞，直到出现逐项匹配声明大小/MIME 的原始字节来源，或产品/source owner 确认原件不可通过当前接口取得并由用户决定是否接受 partial。 |
| BC-32 library 双 ID 被归一化为同一 ID，候选已修正 | 实际 API attachment 对象同时包含 `id=file_…` 和 `library_file_id=libfile_…`。旧归一化把 `library_file_id` 复制进 `file_id`，丢失源 API 的 file identity；若随后按 pointer ID 关联响应，还可能把派生图误标成 library 原件。 | 归一化保留 `file_id` 与 `library_file_id`，MCP handle identity 仍使用 library ID；pointer-only metadata response 不再给 library 项添加 source。回归验证三条 library 仍 unavailable，三条 pointer 独立，35 个定向测试和 97 个全量测试通过。 |
| BC-33 rich-part metadata 未提供原件指针，G2 等待上游证据 | 三个目标图片 part 的 `asset_pointer_link`、`watermarked_asset_pointer`、`dalle`、`generation`、`gizmo`、`segmentation` 均为 null；对应 `asset_pointer` 与消息附件 `id=file_…` 一致。当前页面预加载的 382 个 JS chunk 中，10 个包含 library/download 标记；已观察的生成式 `downloadIntent` 链仍解析到此前测过的派生图。限定在 developers/learn.chatgpt.com 的官方文档搜索未找到个人账户数据导出附件语义。 | 不把 metadata 字段名或文档搜索缺失当作源文件已删除的证据。剩余条件是可信原件路由/字节证据、产品 owner 确认，或用户授权发起账户级数据导出验证；未获授权前不提交该账户操作。 |

本轮接口审计新增（代码证据；尚未修复）：

| ID | 当前证据与风险 | 新计划中的修正及验证 |
| --- | --- | --- |
| BC-34 旧上传兼容路径仍整文件缓冲 | 旧 `/uploads`、legacy Browser API 仍有整文件路径；新 FileStore、tus/Range、HTTPX 内容流、Playwright Path 上传和 ExportStore views 已磁盘/分块化。 | 新 `file_ref` 与 session page spool 有真实/本地回归；旧 `upload_ids` 仍进程内且受小文件上限约束，跨进程恢复未改变；单源页和旧 inline 路径峰值继续由 BC-37 跟踪 |
| BC-35 一次性句柄不能用于续传 | 旧 `download_export_attachment` 仍在 GET 前 consume，无法 HEAD/Range | 新 `chatgpt_file_download_start` 将源附件准备为独立 file_ref，允许重复 HEAD/GET/Range；旧单次句柄保留兼容 |
| BC-36 stdio 与 HTTP 上传状态分属进程 | 新 `file_ref` 和 transfer API 的 stdio 调用代理到 HTTP Broker，文件状态在共同 FileStore；旧 `upload_ids` UploadStore 仍是进程内状态，task worker 仍按现有共享 Scheduler 取得一个浏览器槽 | 新 FileStore 在两个实例间续传/恢复通过；旧 UploadStore 跨进程恢复未改变，兼容旧客户端限制继续保留并说明 |
| BC-37 任务结果一次返回与大源页内存边界分别处理 | 新 `chatgpt_result(delivery="reference")` 与 `chatgpt_result_page` 有 UTF-8/摘要游标；ExportStore views 磁盘化，browser 按源页规范化到临时 spool，再由偏移索引生成 messages/Markdown。source/spool 与快照各有 64 MiB 显式配额，不静默截断。 | 最终全量 126 passed/3 skipped；256 MiB TUS 慢读、写失败回滚、多进程 offset 争用、孤儿 spool 恢复通过。新增 opt-in 合成压力：16 MiB 单消息完整分页重组，spool 33,555,001 bytes、快照 50,332,159 bytes、并存高水位 83,887,160 bytes、tracemalloc peak 134,262,436 bytes；24 MiB 单消息超过快照配额，确认显式失败且不发布半成品。真实会话 106 页 spool smoke 通过。仍未验证真实 ChatGPT 超大页的内存峰值、并发导出总磁盘配额或真实磁盘耗尽；不通过填满工作站磁盘做验证，保留注入写失败回归。BC-37 的本地可安全压力项完成，真实客户端/部署门槛仍开放 |
| BC-38 普通任务幂等缺输入摘要 | Scheduler.admit 旧逻辑对同 request_id 直接重放，忽略任务正文、能力或文件变化 | `queue.request_hash` 已迁移；submit/resume/continue 将 session、prompt、模型参数与 file_ref 纳入输入摘要。Scheduler changed-input conflict 回归通过；跨重启网页发送不确定状态尚待 G3 恢复验收 |
| BC-39 tus 组件不能直接成为当前 Broker 的复用件，已纠偏 | tussi 明确 Linux-only；fastapi-tusd 需要未采用的 FastAPI 且缺过期扩展；TusFastAPIServer 无许可证 | 在 Windows ASGI 实现标准子集；固定版 tus-js-client@4.3.1 上传 48,000 字节，最终 HEAD 的 offset/length/hash/file_ref 全部匹配。独立 npm 包只用于互操作测试，不加入运行依赖 |
| BC-40 独立 Manager 的目标会话导出只到最新 API 页 | 历史 2026-09-26 烟测只读到 51 条，旧页和附件元数据报 `request_context_unavailable`。 | 已由 BC-41 修正后在同一 URL 重测：旧页 status=200、14 条，源页链终止；63 条可见消息 `verified`。25 项附件保持 `partial`，3 个 Library 原件仍无来源；不把消息通过扩大解释为附件通过。 |
| BC-41 页面暴露的请求头缺少 X-OpenAI target route，候选修复通过真实复测 | Playwright `request.all_headers()` 在目标页首屏 200 响应中未暴露 `X-OpenAI-Target-Path` 和 `X-OpenAI-Target-Route`；`_client_api_headers` 错把它们当作必须从认证请求复制的上下文，较早页因此被归类为 `request_context_unavailable`。interpreter 与 attachment metadata 刷新也受同一检查阻断。 | 目标 URL、conversation ID、源 host 和消息路由先严格校验；再由已校验的目标路径生成两个非凭据 route headers，只转发页面暴露的认证/OAI 客户端头，Cookie 仍由同源 credentials 携带。单测覆盖缺头、错误目标头覆盖与 Cookie 过滤；真实同会话读取 51+14 raw、63 visible、2 页、终点通过，sandbox/attachment metadata refresh 均成功。不得把头值写日志。 |
| BC-42 HTTPX 附件流没有浏览器 Cookie，22 项统一失败 | 请求上下文修复后真实 manifest 有 22 个 available/candidate 来源；`_stream_content_to_path` 用无 Cookie HTTPX 请求，代表性同源 Estuary GET 得 HTTP 403。相同最终 URL 用浏览器匹配 cookie 请求返回 200，HTML 附件 MIME/Content-Disposition 与声明一致；不能仅凭 `text/html` 判为错误。 | 从 Playwright Context 只取目标初始 URL 匹配 cookies，按原 domain/path 写入临时 HTTPX cookie jar；仅允许已验证域的手动重定向，域不匹配的 ChatGPT cookie 不转发。单测验证跨域不泄漏；目标会话 22/22 实际物化通过长度/MIME/SHA，共 7,270,631 bytes；3 个 Library 原件仍不可用且维持 partial。cookie/signature 不落盘或日志。 |

## 文件往返验收补充（2026-09-24）

- **消息历史**：`scope=all` 读取必须继续到源站最早页标记；固定页数不作为成功条件。若游标不前进、循环、页面偏离或 API 错误，则显式 partial/failed，不返回“全量”状态。
- **客户端分页**：一个 READY 快照的全部消息对象都必须能重复读取到 EOF；页数计数用于审计，不再作为硬拒绝阈值。单页大小、快照总量、TTL 是显式资源政策；达到容量时失败，不静默省略内容。
- **ChatGPT→客户端文件**：附件 manifest 保留每个观察到的引用及归属；客户端下载每个 available 项，核验字节数/摘要。不可下载项保留在清单并阻止附件 verified。
- **客户端→ChatGPT 文件**：客户端传原始字节到受认证的 `/uploads`，用返回的短期 upload_id 在 submit/continue/resume 请求中引用；服务端经 Playwright chooser/input 传入会话并验证附件 receipt，任务结束清理内存句柄。当前上传大小与数量上限继续对客户端可见。
- **UI 文件元数据扫描**：若 source API 不带 interpreter 下载元数据，可在当前 export 自己的临时标签页触发已观察到的 metadata 请求；只用稳定 message/sandbox/file 标识刷新文件，不用 UI 滚动读取消息页，也不操作用户现有 tab。
- **浏览器 tab 隔离**：必须复用 React 卡片动作时，只在专用临时页渲染/滚动并在下载后关闭；不导航、滚动或关闭用户已有会话标签页。消息分页路径不得依赖这个附件回退。

## 阶段 0 出口记录（2026-09-24）

- **G0：通过。** 固定版本的 Playwright 导出参考与 MIT 内容/文件模型参考已逐段核对；指定 Chrome 扩展仍只有行为级资料，没有源码，未假装已借用其内部算法。
- **目标页协议**：当前页面 JS 的 v6t 调用 safeGet GET /conversations/{conversation_id}/messages，传 before、include_has_versions、num_turns；p6t 用 has_previous_page 和 start_cursor 控制向旧页迭代，并检测游标是否前进。请求选项使用 SendIfAvailable。当前 latest 请求为 200、51 条、has_previous_page=true、has_next_page=false；复用该页动态客户端头、无滚动请求 before 后为 200、14 条、has_previous_page=false、无重复，共 65 个唯一 ID。该实测没有读取或输出正文、游标值、鉴权值或签名参数。
- **附件证据**：当前页面的 interpreter/download 返回 application/json 元数据；estuary/content 单独返回 image/png 和 text/html。BC-05 已确认是句柄来源与最终内容端点不匹配，实际文件字节校验留在阶段 2。
- **复用决策**：保留项目现有 Playwright 1.58 与登录 Page/CDP。调用当前实测的 messages 路由，使用最新成功页面请求的 Authorization/OAI/X-OpenAI 动态头在内存构造 fetch，并更新 target-path/target-route；Cookie 由同源 credentials 携带。按 JS 保留 include_has_versions 和 num_turns。参考项目只提供 401/403 刷新与 429 Retry-After 模式，不复制 Selenium/旧 conversation/{id} 分页实现，也不增加新浏览器或分页依赖。没有证据的 after 路径不做。
- **运行状态**：本机 127.0.0.1:8787 仍由 PID 7208 监听；ChatGPT Oracle 计划任务为 Ready；browser.py 更新时间晚于 server.py，当前生产进程不是本地候选。继续本地验证，不重启该进程。
- **阶段 1 入口复盘**：目标是只替换页请求触发方式，保留现有顺序、去重、边界和失败标记；新增覆盖路由构造、header 过滤、401/429、坏响应、游标重复和 URL 不变的聚焦测试。请求失败时保留 partial/unverified，不转回滚轮分页。

## 设计纠偏与待冻结契约

1. **源站读取先行**：只读分析目标页面 JS 与真实网络请求，确定 conversation/messages 的路径、游标字段、必要客户端上下文和响应边界。随后在已登录页面上下文中直接按已观察到的 start_cursor/before 请求更早页；若确需读取更新页，先实测其请求形式再实现。不靠 mouse.wheel 触发 API；每页核对目标 conversation URL、请求游标、响应 page_info 和相邻页 ID。页面 URL 偏离目标即中止。页面私有实现变化时明确 partial/失败；DOM 只保留为标明 unverified/partial 的诊断回退，不把滚动当常规 API 分页。
2. **固定来源顺序而非猜测父链**：保留源页顺序、role、message_type、parent_id、可见性和原始 ID；父链不完整时不凭它删除消息。记录原始节点数与面向阅读的可见消息数，分别解释 system/tool 节点的处理。导出期间若会话发生新增或编辑，以页边界和首尾锚点发现变化，有限次重读或标记 partial，不拼接不同时刻的页。scope=recent 只在顺序可信的快照上按 user 回合截取；scope=all 表示本次源读取的全部记录，并附完整性结论。
3. **附件独立闭环**：从每页消息和独立文件元数据请求建立附件索引，记录稳定的 conversation/message/file 标识、类型、归属及来源状态。消息页达到历史终点不自动使附件 verified；只有附件引用枚举和必要元数据也能核对终点时才升级。文件句柄按需从稳定标识创建，下载时重新执行已观察到的页面文件动作或认证 API 请求，监听并校验最终文件响应；元数据 JSON 不能当文件字节，过期签名 URL 不能留作长寿命来源。没有可靠入口时返回明确的不可用原因。
4. **简化交付契约**：每个 export_id 对应不可变、绑定 session 的有序快照。模型读取优先返回含完整消息对象的结构化页、next_cursor 和可核对摘要；单条超长消息才使用带序号与哈希的分段。附件安全元数据随消息或按需索引返回；Markdown 从同一快照按需生成归档文件，不强制客户端读取三份字节流。归档文件由受保护的 HTTP 端点交付；公开操作优先走 MCP，本机 Broker 转发保留为内部通道，额外公开 REST 操作须有独立需求。现有三视图 V2 是兼容候选，迁移前保留，不能先删旧客户端所需路径。
5. **按实测确定调用和限额**：浏览器仍共享任务槽位；保留 start/status 的决定以真实直读耗时和 MCP 超时为依据。显式 request_id 用于客户端重试并绑定输入，不按 URL 自动合并不同时间的导出；V2 新调用优先只接收 session_id 或 conversation_url，task_id 解析留在兼容层。分页限额以 stdio、Streamable HTTP 和 cc-switch 的原始 MCP 响应实测为准，不先冻结当前 8/16 KiB 数值；客户端无需在模型上下文中手工拼接无效 JSON 片段。相同游标重试必须得到相同内容，续聊不改变旧 export_id 的顺序。
6. **生命周期与运维边界**：release 仅清理已完成快照；运行中取消使用独立且可真正中断页面读取的动作，验证浏览器槽位在约定时限内释放。若目前做不到可靠中断，先不宣称“立即取消”。快照正文继续只在 Broker 进程内短期保留；重启后的 READY 记录必须明确转为 GONE 并允许用新 request_id 重导出。为队列、快照实际保留量、SQLite export_jobs/export_events 和幂等记录设置有文档的上限及保留期；TTL 清理必须记脱敏终态，逐页审计只留必要汇总，日志轮转与业务审计分别验证。容量指标要说明计入哪些字节，兼容旧内联 payload 也须计入。
7. **富内容逐类扩展**：先保证源消息、文本、代码、引用和附件关联能无损归属；未知内容类型保留可解释的类型标记并报告序列化缺口。Canvas、公式、表格和 Deep Research 分别以真实样本和回归夹具补充，不因一个统一模型的预设而推迟直接读取与完整性交付。

## 每阶段入口与出口复盘（不得跳过）

- **入口**：阅读上一阶段验收记录和所有未关闭 badcase；核对本阶段适用的参考源码、目标页面实测和当前实现。写明至少一个更简单的复用/薄适配方案，并解释新增状态、接口或浏览器动作的必要性。预先列出成功路径、失败/重试、资源清理、权限边界和最小验证目标。
- **决策**：在下表留下参考版本与位置、复用/自实现边界、被否决方案及证据、预期风险和验证方法。源码不可得时注明行为证据能支持到哪里；没有证据的私有 API、分页参数或附件地址不得进入实现。入口记录未完成，本阶段保持“待复盘”，不开始代码改动。
- **出口**：对比预期与实际，记录测试和真实源站证据；新增或关闭 badcase，判断有没有无谓的 UI 依赖、协议重复、状态膨胀或未测试的回退。发现与参考/源站冲突就更新设计并重测本阶段，不能仅因代码写完而进入下一阶段。同一阻碍连续失败时遵守项目 AGENTS.md 的探索与阻碍记录规则。

| 阶段 | 入口决策记录 | 出口复盘与下一阶段许可 |
| --- | --- | --- |
| 0 参考与基线 | 已完成：固定版本开源对照；目标页 v6t/p6t 游标调用；动态请求头只在进程内重放 | G0 已通过：不滚动直请求 51+14，无重复、两端边界已观察；生产 Broker 与候选分开 |
| 1 直接读取 | 入口/出口复盘完成：使用现有 Playwright Page/CDP；按 safeGet 的实测 route/query 做薄适配；动态头仅在内存；API 页序优先于时间戳 | 通过：7 项聚焦测试；目标 2 页，51+14 原始节点，65 唯一 ID，63 可见消息；规范化顺序与源数组一致；messages verified、attachments unverified；api_paged 无滚轮。进入阶段 2 |
| 2 附件 | G2 未通过；当前复核确认扫描覆盖判定不足，3 个图片候选失败、3 个 library 引用无来源 | 保留已验证 HTML 与明确 unavailable 状态；先按已派发的只读审计结论修正来源发现/覆盖，再对同一目标逐项核验大小/MIME/SHA。任何缺项仍为 partial/unverified |
| 3 交付 | 入口复盘完成；出口本地通过：保留 start/status（实测源读取约 8–24 秒）；MCP messages 页用 structuredContent 返回完整对象，单条超长消息单独分段；归档走受保护文件端点 | MCP E2E 收集器的内存 ASGI start/status/pages/release 多页摘要校验通过；目标远端客户端仍待验收；G2 未通过时不得称附件全量 |
| 4 审计资源 | 旧候选入口/出口复盘完成；本轮纠偏重新打开：复用 Scheduler SQLite 聚合计数与终页审计，不再以固定页数拒绝后续页；扩充上传与附件判定回归 | 本地全量 79 passed；1,002 源页、4,097+ 客户端页、附件 HTTP 摘要、submit/continue/resume 上传和本地 Chromium 原字节回归通过。真实附件覆盖、生产 Broker、FRP 与公网尚未验收，G2/G4 不通过 |
| 5 客户端发布 | 未进入：等待 G2 附件、G3 真实客户端和 G4 生产/公网门槛；WebDAV 同步由用户验证 | 待验收：旧客户端迁移、纯净 Luna、公网结果、回滚依据和剩余 badcase |

## 阶段 4 出口记录（2026-09-24）

- **简化/复用决策**：沿用现有 `Scheduler` SQLite、单 slot 队列、事件 allowlist 与轮转日志；只为 `export_jobs` 增加 `page_requests` 迁移列。不引入第二个数据库、独立调度器或 OS 定时任务。独立 asyncio maintenance task 复用现有 Manager 生命周期，间隔由快照 TTL/审计保留期推导，最多 30 秒。
- **资源和保留**：`CHATGPT_EXPORT_AUDIT_RETENTION_SECONDS` 默认 7,776,000 秒（90 天）；裁剪旧终态 request envelope、事件和关联 export queue 行，活动请求保留。等待队列（不含活动 slot）最多 8 项；超限返回 `CHATGPT_QUEUE_FULL`。每 export 的分页调用只累计在 SQLite `page_requests`，不设固定拒绝页数，也不为每个中间页插入一行 event；view EOF 事件包含最终累计数、字节数和摘要。重复/无效游标仍计数。
- **生命周期**：无访问到期由后台清理内存快照/句柄，并将 SQLite 状态置为 GONE、写入脱敏终态事件。Broker 启动时，持久 READY 但进程内快照不存在的项主动标 GONE；QUEUED/RUNNING 继续恢复。MCP 与 HTTP export dispatch 均启动 Manager，因此重启后仅查询 status 也会启动队列 worker。
- **取消边界**：运行导出由独立子任务执行；cancel 取消当前可中断的 Playwright await，并最多等待 5 秒确认 slot 释放。超时仍报告 `CANCEL_REQUESTED`，不抢占 slot。正常 Manager 停止会将未显式取消的运行导出重新排队。release 对 QUEUED/RUNNING 返回 `EXPORT_NOT_READY`，仅释放 READY 快照。
- **纠偏验证**：当前候选全量 `\.venv\Scripts\python.exe -m pytest -q`：**79 passed**。覆盖 1,002 页源站算法、超过旧 4,096 次客户端 EOF、附件响应 Content-Length/SHA-256、SQLite 字节审计、HTTP upload→upload_id→submit/continue/resume，以及本地 Chromium filechooser/input 精确原字节/MIME/文件名/SHA。
- **出口结论**：上传导入有本地闭环证据；真实 ChatGPT 附件来源和清单覆盖仍不完整。当前候选不再仅凭 image asset pointer 生成文件下载 URL；若 UI 渲染数量低于 API 消息数，则标附件 partial。未部署，G2/G3/G4 不得标通过，不进入客户端发布。

## 历史目标会话复核（2026-09-24）

- **消息读取**：direct API 同次读取 2 页、65 个原始节点、63 条可见消息，源端页链终点通过，消息状态为 verified。
- **附件读取**：最终当前候选脱敏读取为 8 项：6 个 PNG 引用 unavailable，2 个 interpreter HTML 有来源且此前已按长度/MIME 校验；裸 asset pointer 不再生成下载候选。不同扫描轮次捕获 2 与 4 个 interpreter metadata 响应，不可当作附件总数。
- **扫描覆盖**：最终读取 `stable=true` 但 DOM 只渲染 8/63 条；新增最低覆盖检查返回 false，附件 completeness 为 `partial`，并非完整清单证明。
- **独立审计结论**：三种引用不能共用同一种下载定位规则；`content_references` 保留为 citation/deep-research 元数据，不因其中出现 `library_file_id` 就自动升级为二进制附件。只有观察到可靠文件来源后才复用通用字节校验/HTTP 传输层。
- **剩余工作**：当前补丁只收紧误导性候选和不足覆盖时的状态，不实现猜测性 URL。仍须完成覆盖驱动的附件枚举、逐引用来源关联与真实字节传输；拿不到源站来源时，如实保留 unavailable 并阻止 G2。真实 ChatGPT 上传、公网 Broker 和客户端部署另列验收，不由本地 Chromium 测试替代。

## 当前目标会话复核（2026-09-25）

- **候选运行边界**：最终 E2E 在候选代码的临时 Streamable HTTP app（127.0.0.1:8789）执行，使用独立临时 SQLite/Session 目录；没有重启或替换 8787 Broker。此前 stdio E2E 实际代理到 8787，已按 BC-25 排除在候选验收之外。候选未部署。
- **消息交付**：同一次候选读取 direct API 两页、65 raw 节点、63 visible messages，页链两端终止且无重复。MCP messages view 以 106 页重组为 826,876 bytes，SHA 一致，单页最大 HTTP wire response 为 15,400 bytes；messages=verified。
- **附件清单**：同次 manifest 有 25 项：3 个 api_attachment、3 个 image_asset_pointer、16 个 interpreter_output、3 个 library_file。成功 API 路径直接枚举完整消息中的普通附件、pointer 和 `sandbox:/mnt/data/` links，不扫描/滚动整页 UI。
- **附件传输**：22/22 个有来源项经 MCP 下载，共 7,461,877 bytes；每项 Content-Length、MIME、SHA-256 对齐，failure_count=0。3 个 library_file 原件无可验证的原始字节来源，unavailable_count=3，attachments/overall=partial。
- **sandbox artifact 来源**：16 个 assistant message links（3 HTML、2 ZIP、2 JSON、2 Markdown、7 PNG）均经精确 message_id+sandbox_path metadata 路由返回 success；浏览器内存流式复核 16/16，5,850,463 bytes，名称/MIME/终止状态与响应对应，未写入本地文件。metadata 未提供原始大小字段，MCP E2E 对实际输出补做长度/MIME/SHA 校验。
- **library 原件来源**：目标 user message 的 attachment metadata 同时给出 `id=file_…` 和 `library_file_id=libfile_…`，两者不能混作一个 ID。用户指出的精确 `GET /api/library/files/{library_file_id}/download` 已单独直测三项：初始均 302，脱敏 Location 指向 `chatgpt.com/backend-api/estuary/content`，最终 200 `image/png`；实际长度为 310,807/311,171/183,729，SHA 与会话 inline pointer 完全相同，均小于 API 声明大小 512,134/494,544/357,736。无 Content-Length，Content-Disposition 使用原文件名。三个 rich-part 的 `asset_pointer_link`、`watermarked_asset_pointer` 和生成/转换 metadata 均为 null；代码继续将三条 library 原件标记 unavailable，不能把派生图当原件，也不能说文件已删除。G2 仍阻塞。
- **身份映射回归**：当前候选重新读取目标会话仍得到 63 条 messages=verified、25 个附件项（3 library、3 pointer、16 interpreter、3 api_attachment）；三个 library 记录保留独立 `file_id`/`library_file_id`，无 source/handle。身份修正后的隔离 MCP E2E 再次完成 106 页 messages（826,855 bytes）、1 页 attachments、17 页 markdown；22/22 available 附件传输通过，0 failure，3 library unavailable，状态保持 partial。收集器因明确的 partial 门槛返回 exit 1，属于预期结果，不是传输错误。
- **agent 视觉可用性**：三张实际 MCP 可用的 pointer PNG 分辨率为 2048×1018、2048×1014、2048×1016。直接视觉检查可辨认 NCU 临床训练主界面、沟通区、病历/医嘱工作台、主要控件和表单字段；没有明显缩放或模糊问题。最小号浅灰辅助文字不保证逐字 OCR，且没有原始字节，不能声称像素级保真。按当前用户目标，G2-visual 通过；G2-byte 继续记录为 partial。
- **审计与状态**：来源审计仍只存脱敏计数/字段类型，不存 ID、文件名、URL、签名参数或正文。全量本地测试 97 passed。生产 8787 与 CDP 9222 未重启；公网、FRP、真实客户端和上传至真实 ChatGPT 尚未验收。
- **上传方向**：`POST /uploads → upload_id → submit/continue/resume → Playwright file input/chooser` 的本地精确字节验证仍通过；此 E2E 未替代真实 ChatGPT 上传与生产 Broker 验收。

## 阶段 2 续作入口复盘（2026-09-25，直接引用路径）

- **证据更新**：DOM scan 两轮后仍只有 14/63 IDs，且 interpreter response 数在不同运行中变化。完整 API message pages 本身含 16 个 assistant sandbox links；已逐项请求 source metadata 并做流式内容检查。全页 UI scan 不再是成功 API export 的附件发现前置条件。
- **简化/复用决策**：复用现有游标 API 页面、`_authenticated_page_json_get`、`_client_api_headers` 和已观察的 interpreter/download 路由。对普通 API attachment 以源记录 id/name/size 校验短期内容 id；对 asset pointer 复用已实测 `download_intent=false&inline=false` 路由；对 sandbox links 从源 message text 提取精确 message_id+sandbox_path，再刷新 metadata。签名 URL 只留在请求内存中；不新增数据库或浏览器依赖。
- **library 专项审计**：实际资料库列表确认三项仍 available；目标消息里 `id=file_…` 与 `library_file_id=libfile_…` 是两种标识。真实 Library `downloadFile` 动作、正确会话上下文的文件 metadata→content 路径和 inline pointer 逐项得到相同派生图。第一项 downloadIntent=true 分支也得到派生图。候选已修正双 ID 归一化，并禁止 pointer-only response 关联为 library 原件；manifest 继续保留 library refs 与 pointer refs 两项，原件 unavailable。
- **失败与边界**：sandbox metadata 或签名内容 ID 与 message link 不匹配时保留 unavailable/partial；library 原始字节大小不符时不降级为 pointer derivative。正常 API 导出不滚动 UI；API 不可用的 DOM fallback 仍明确标记 partial/unverified。
- **验证目标（当前用户范围）**：已验证 22 项 MCP 文件字节传输；当前下一步是由真实 MCP 客户端把三张 available pointer PNG 交给 agent，并确认其能识别界面结构/主要字段。停止为当前目标继续探测 OpenAI 原件 endpoint 或账户导出。G2-byte 仅表示归档原始字节完整性，保留 partial；仅当用户重新要求原件归档时才重开。

## 阶段 2 续作出口复盘（2026-09-25，API 引用闭环）

- **完成的纠偏**：普通 attachment id 与 asset pointer 分别按已观察 route 验证；manifest identity 加入 file kind 和 file/library ID，防止同消息 id alias 把 library 原件并入 pointer。成功 API export 不调用整页 UI scroll；assistant sandbox links 直接来自完整 API messages，并解析为稳定的 interpreter metadata requests。
- **本轮身份纠偏**：保留源 API 中不同的 `id=file_…` 与 `library_file_id=libfile_…`；pointer metadata response 不再给 library item 赋予下载源。当前目标 direct API 回归维持 63 verified messages、25 项附件分类，并确认三条 library 项仍不可下载。35 个定向测试、97 个全量测试通过；身份修正后的隔离 HTTP MCP E2E 已重新验证 22/22 available 下载、0 failure 和预期 partial 状态，未部署。
- **附件分类与状态**：候选 manifest 25 项，分为 3 api_attachment、3 image_asset_pointer、16 interpreter_output、3 library_file。22 个有来源项经 MCP 实际传输 22/22 成功，7,461,877 bytes，逐项 Content-Length/MIME/SHA 校验通过；三个 library_file 明确 unavailable，attachments/overall=partial。
- **消息与工具验证**：同次候选读取 messages=verified，63 visible/65 raw，2 源页；MCP 消息 view 106 页重组且 SHA 一致。全量 pytest 97 passed；38 项附件定向测试通过。
- **复盘结论（按新验收范围）**：22 项有源附件的 MCP source/transport 闭环已验证；三张 Library 原件字节仍不可得，但对应 pointer PNG 可下载，视觉检查认为足够用于当前 agent 识别。当前不把“找回原始上传 PNG”作为客户端验收阻塞；仍要保持 API 的 archive completeness 为 partial，不把派生表示称为原件。
- **精确续作条件**：下一步进入 G3：用真实目标 MCP 客户端读取目标 session，确认三张 pointer 图按原 user message 归属被交给 agent，并验证其能识别屏幕大区、关键控件和主要字段；测试不提供对话正文作为图片识别答案。G2-visual 通过后可按当前目标进入 G4 候选验收；G2-byte 继续 partial。停止账户导出和额外原件 endpoint 探测，除非用户重新要求原始字节归档。生产 Broker、FRP、公网与回滚仍需单独验收。

## 按需补充文件协作设计（2026-09-26）

**目标与交付范围**：agent 能说明哪张图的哪个区域看不清，并使用完整文件、客户端保存的原文件或更清晰的局部图继续同一任务。本轮交付 skill 备注及协作设计；真实客户端读图、补充文件往返和公网发布仍待验收。已有三张 pointer PNG 的视觉复核结果保留，不因为该设计重复原件 endpoint 研究。

**复用证据**：`server.py` 已有 `chatgpt_export_attachment_handle(session_id, export_id, attachment_id)`、受保护 GET 下载、`POST /uploads` 和支持 `upload_ids` 的 `chatgpt_resume`/`chatgpt_continue`。`exports.py` 的附件清单已有消息归属、`file_kind`、可用性；`uploads.py` 返回文件名、MIME、大小、SHA-256 和 TTL。已有 `test_attachment_download_returns_digest_and_persists_transfer_audit`、`test_uploads_reach_submit_continue_and_resume_jobs` 覆盖下载摘要及上传到同一会话的本地路径。此设计不新增服务端协作状态机、原件恢复接口或重复的文件存储。

### 文件含义与职责

- **完整下载文件**：实际下载到的二进制文件，避免仅使用客户端缩小的预览。它可能仍是 ChatGPT 的 pointer 派生图；直接下载与原始上传字节不是同一项保证。
- **原始文件**：客户端保存的原始上传文件，或来源有原件依据的文件。原件是否可用逐项判定；大小、文件名或下载成功本身不证明原件存在。缺少原件时仍可使用已有表示或用户提供的清晰局部图，不回到账户导出。
- **客户端/agent**：判断当前任务是否需要更多细节，选择有依据的文件来源，关联原消息与目标区域，将文件交给实际支持的图片/文件输入能力。清晰度评价可以用自然语言描述，无需新设模型状态或数值阈值。
- **MCP**：按既有句柄与认证传输字节，报告大小、SHA-256、错误和页面附件证据。下载成功、上传成功、页面显示附件和 agent 看懂内容分别成立；服务端上传摘要不证明 ChatGPT 内部保留或向模型呈现原始像素。
- **skill**：说明这些选项、参数、可用性和边界。可读的图片可以直接继续，不按任务关键词、文件大小或 `overall=partial` 强制调用/重传/升级模型。

### 双向流程与结束条件

| 接收方 | 文件路径 | 交付确认 | 识别确认 |
| --- | --- | --- | --- |
| 本地 agent | 当前导出 manifest → 对应短期下载句柄 → 校验后的文件 → 客户端图片/文件读取工具；也可读取客户端已持有的原文件 | GET 响应长度/SHA-256 与实收字节一致，图片读取工具实际接收文件 | 依据图片回答目标区域的问题；不能只把 URL、base64 或元数据当成已看图 |
| ChatGPT Web | 选定文件字节 → `POST /uploads` → `upload_id` → 同一 `session_id` 的 `chatgpt_resume(upload_ids=[...])`；沿用任务也可 `chatgpt_continue` | 上传回执摘要匹配所选文件，目标 conversation URL 正确，页面附件验证通过，任务完成 | 取回回复，确认先前看不清的区域是否得到有依据的回答 |

补充 prompt 可包含“对应原消息/文件、当前文件来源、目标区域、需要辨认的内容”。例如：“补充客户端保存的原始上传文件 orders.png，对应上一条医嘱截图；请查看右侧剂量栏，无法确定的字符请明确指出。”这些是普通 prompt 内容，不新增必填 API 字段。手册里的句柄、`upload_id` 和 `request_id` 各司其职；同一次发送的重试复用既有幂等机制，补充了不同文件/问题的下一轮使用新的 request_id。

已满足识别目标即可结束补充。若直接下载与当前图 SHA-256 相同，重复下载/改名/重传不能增加细节；只有客户端预览缩小的情况可能通过实际图片读取工具改善。仍缺细节时选择有更多信息的原文件或清晰局部图。没有这样的来源时返回具体不可辨认区域与缺少的文件，由用户提供来源后继续；不猜测、不隐瞒缺口，也不阻断无关的已可识别部分。句柄过期可重新领取，原件不可用则说明来源缺失；二者不能混为同一种重试。

### 落地顺序与验证

1. **手册落地（本轮）**：仓库、本机 cc-switch skill 库和 Codex 安装副本加入一致的附件备注，去掉按任务类别强制调用的措辞；保留各副本现有接口说明，候选 V2 能力以已连接 MCP 工具列表为准。验证 frontmatter、备注一致性、接口参数与当前代码对应关系。WebDAV 同步由用户验证。
2. **G3 真实客户端验收（待执行）**：沿用隔离候选，从目标 session 下载已有 pointer PNG，交给真实客户端图片输入；再使用客户端保留原图的受控样例验证补充原文件。限定具体区域的问题，记录实际图片输入、同 session 往返、传输摘要、页面附件证据和回答；不给测试 agent 对话正文或参考答案。先验证已有组合能闭环，观察到明确缺口才增加客户端辅助代码。
3. **独立使用及 G4 发布（待执行）**：纯净 Luna 仅获得 skill、任务和可访问文件，验证能自主选择直接识别或补充文件；通过后按现有计划做生产、公网与回滚验收。此设计不把既有 97 个测试或视觉复核冒充真实文件往返通过。

| 验证场景 | 验收结果 |
| --- | --- |
| 当前图已能回答任务，即使 Library 原件 unavailable | agent 能完成识别，手册不要求为 partial 状态补传 |
| 客户端只展示缩略图，但可下载文件足够清晰 | 经下载校验和实际图片输入，读出指定区域；接收端支持文件输入是前提 |
| 派生图不足、客户端保留原文件 | 交给本地 reader 或按目标接收方上传并续问；原消息/文件对应正确，摘要一致，目标细节识别通过 |
| 下载文件与既有图相同、无更清晰来源 | 不把再次下载称为获取原件，不以相同字节循环补传；报告具体缺口与可继续条件 |
| 新文件发回 Web | 同一 conversation URL、附件证据、任务完成与识别结果逐项确认；上传成功不代替识别成功 |
| 句柄失效、发送重试或客户端不支持图片输入 | 复用已有过期/幂等语义；无重复消息、无串 session，明确报告接收客户端能力缺口 |

**本阶段出口**：仓库、本机 cc-switch 和 Codex 三份 skill 的 frontmatter 校验通过，附件备注一致，接口参数已与代码核对，按任务类别强制调用的措辞已移除。本轮交付手册和设计；G3/G4 保持待验收。G2-byte 的 partial 不因原文件“选项”而改变；将来客户端提供某个原件也只说明该来源可用，不倒推 OpenAI 端原件已恢复。

## 执行顺序与阶段产物

| 阶段 | 当前状态 | 改动与产物 | 进入下一阶段的门槛 |
| --- | --- | --- | --- |
| 0. 参考研究与基线 | G0 已通过；阶段 1 入口复盘完成 | 固定版本开源源码、指定扩展公开行为、当前页面 v6t/p6t 与请求构造、51+14 无滚动直请求、附件元数据与内容响应均已记录；核实 Broker 与测试状态。 | 候选和生产差异已列明；旧缓存、DOM 与 API 观察分开；历史 65 条不作为未来固定总数。 |
| 1. 直接 API 读取 | 目标会话历史实测通过；纠偏后本地游标迭代无固定页数上限 | 复用 Playwright Page/CDP，按当前页面 JS route/query 直接构造 before 请求；动态 OAI/X-OpenAI 头只在内存；以 has_previous_page=false 结束，重复 cursor/URL 偏移标 partial。DOM 回退仍不宣称完整。 | 目标两端页边界可信、65 个原始 ID 唯一、63 个可见消息保持源数组顺序；1,002 页合成回归通过；真实目标读取仍需重新验收。 |
| 2. 附件发现与下载 | Agent 视觉表示 G2-visual 通过；原始字节归档 G2-byte 仍 partial | 完整 API pages 是附件 inventory 输入；从 metadata attachments、image_asset_pointer 和 assistant sandbox links 逐类取源。成功 API 分支不做整页 UI 滚动；source URL/签名 URL 仅在进程内，审计只存脱敏计数/错误类型。library_file 与 pointer 保持分项；agent 识别使用 available pointer PNG，不将其标成原件。 | 目标会话隔离 HTTP E2E 22 项 available 传输通过；三张 PNG 视觉复核适合识别界面和主要内容。当前目标无需追求逐字节原图；若以后声明原始归档完整，再要求 library originals 与源 API size/MIME 对齐。真实客户端仍需确认 MCP 端能收到并使用这些图。 |
| 3. 收敛交付接口 | 本地候选通过；agent visual representation 已有源端和 MCP 字节证据，真实目标客户端待验收 | MCP messages view 返回 structuredContent 对象页，游标按 ordinal 绑定；超长消息有 offset/hash 分段。受保护文件端点负责归档；legacy byte views 只保留过渡兼容。内部 tool JSON 限 15 KiB，wire 尺寸实测低于 16 KiB。 | 在真实 Streamable HTTP/stdio 与 cc-switch 上验证消息无截断、pointer PNG 可获取且 agent 识别通过。该目标不要求原始 PNG 字节；MCP manifest 仍如实标记 Library original unavailable。 |
| 4. 审计与资源闭环 | 本地候选代码回归通过；生产运行验证待做 | 保留 SQLite/日志、90 天可配、队列 8、TTL GONE、READY-only release 和独立 cancel；来源摘要 allowlist 记录覆盖/引用计数，不记值；READY manifest 有 unavailable 时强制 partial。 | 最终全量 126 passed/3 skipped；256 MiB 文件压力及两项大消息 opt-in 压力均分别通过。目标源 22 项可用附件校验均有证据。3 个 Library 原件、真实客户端、公网、生产 Broker 和真实 ChatGPT 上传仍未验收；真实磁盘耗尽未测，写失败注入已覆盖。 |
| 5. 客户端与发布 | 附件协作手册本轮更新；发布未进入，G3/G4 待验收；G2-byte 为非当前目标的 partial | 仓库与本机 cc-switch/Codex 副本已加入原文件补充选项；后续纯净 Luna 只获公开手册与任务；公网 FRP 验收及回滚演练。WebDAV 同步由用户验证。 | 完成真实客户端视觉识别、按需文件往返和无截断验收后，进入候选部署、FRP/公网验收及回滚演练；skill 只说明能力与使用方法，不硬编码模型调用时机。 |

## 验证矩阵与发布门槛

- **参考与纠偏**：阶段 0 记录指定扩展的行为证据、开源代码固定版本与许可、目标页面 JS/网络证据、复用边界以及被否决的复杂方案。阶段 1-5 每次开始前填写入口决策、结束后填写出口复盘；以 badcase ID 关联失败现象、修正和回归，不以“代码存在”代替通过。
- **源端**：tests/test_export_extraction.py 增加游标连续、终点、重复/冲突 ID、不完整 parent 链、会话中途变化和页面 URL 偏移；以同一次目标会话 API 原始页与规范化结果比对。较早的 65 个原始 ID 只作历史参考；当前完整性必须由当前两端页边界证明。
- **附件**：为 API 引用与 interpreter 元数据分别造夹具，并对真实 PNG/HTML 和另一条已知文件会话的 ZIP 逐类下载验证；比较文件字节摘要、归属、失败状态和未观察到的预期文件。消息 verified 而附件 unverified/partial 的情况必须可表达。
- **交付**：扩展现有 tests/test_export_paging.py、test_export_lifecycle.py 和 test_export_http.py，覆盖跨 session、长消息、重试、失效游标、容量、重启及真实 MCP 封装。scripts/export-e2e.py 只记录脱敏 ID、页数、字节、哈希与错误码；模型实际阅读时要能收到可解析的消息页，归档时要能下载和校验文件。
- **运行**：测试轮转日志与 SQLite 事件保留/清理、无访问情况下的 TTL 到期审计、队列上限、取消时限和双进程归属。聚焦测试针对改动批次运行，最终候选全量 pytest 至多一次；若候选改变或有新失败，再进行针对性复测。
- **客户端**：经 stdio、Streamable HTTP、cc-switch 和 neko-dashboard.com:32751 对应的现有 FRP MCP 入口检查工具发现、鉴权和原始响应。纯净 Luna 仅获公开 skill 手册与任务，观察其是否能自主判断何时使用、是否会逐页读至终点，并如实报告未知；不向模型注入调用时机规则。公网测试在候选通过本地门槛后进行。

发布记录按门槛分别判定：G0 为参考矩阵、复用决策、badcase 登记和阶段 0 出口复盘完成；G1 为目标会话直接 API 页链与消息顺序可信；G2-visual 为附件可由下游 agent 接收并完成目标识别；G2-byte 为附件原始字节清单和下载来源完整；G3 为消息分页和附件在真实客户端无截断；G4 为审计、资源、故障恢复和公网调用可信。当前用户目标只要求 G2-visual，不要求 G2-byte；系统仍须把二者分别报告，不能把视觉可用性写成原始归档 verified。每个后续阶段还须有自己的入口/出口复盘。若附件无法证明覆盖或源站缺历史终点，可以交付已观察内容并标明 partial/unverified，不能把传输成功写成全量成功。参考扩展样本只用于差异分析，不代替源端证据。

最终验收记录应列出参考与复用决策、各阶段复盘、未关闭 badcase、同一次读取的源页数、原始节点/可见消息数、附件各类计数、终点证据、交付摘要、审计事件与失败原因，不附对话正文或凭据。富内容的未支持类型、目标会话未观察到的 HTML/ZIP 与 WebDAV 用户验证分别列为独立结果，不隐含在“通过”二字里。

## 预期效果

直接游标请求消除正常导出的滚动等待和页面跳动；消息与附件分别有来源和终点证据；模型能按可解析的消息页阅读，用户能直接下载一致的归档文件；异常状态有有限资源占用和可审计恢复。源站没有提供的消息或文件仍无法被补造，此时结果必须准确说明可见范围。
