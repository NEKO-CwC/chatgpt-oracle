# ChatGPT Web 文件上传复用调研

日期：2026-09-16

目标：为当前 `chatgpt-oracle` 的正式 Chrome + `127.0.0.1:9222` Worker 增加文件上传能力，优先复用已有实现和经过验证的流程。

## 当前本地实现边界

当前 Worker 使用 Python Playwright 连接正式 Chrome 的本地 CDP，MCP 只暴露语义级任务工具。文件上传采用客户端经 FRP 传到服务端的内存流，再由 Worker 传入 Chrome；不接受客户端本机路径，也不把文件先写入 Windows 文件系统。

因此可复用的实现必须满足：

- 能在现有登录 Profile 和本地 CDP 上运行；
- 不读取 Cookie、localStorage 或密码；
- 上传流能校验文件名、大小、MIME、SHA-256 和内存配额；敏感内容策略由客户端负责；
- 上传后能确认附件 chip/文件名出现；
- 发送任务时保存文件名、大小、SHA-256 和上传结果；
- 不把文件正文写入 MCP 日志或普通任务状态。

## 项目对比

### 1. `pauljunsukhan/codex-chatgpt-pro-plugin`：最适合复用运行时代码

它和我们的浏览器底座最接近：持久登录 Profile、CDP `127.0.0.1:9222`、全局浏览器锁、Pro 调用、文件上传和 receipt。

它的 `src/chatgpt-upload.mjs` 已经实现了：

- 校验文件存在、普通文件、大小和 SHA-256；
- 为文本文件创建带时间戳和原始文件哈希的暂存副本，避免 ChatGPT 的重复文件拒绝；
- 通过 `DOM.setFileInputFiles` 写入 ChatGPT 的 `#upload-files`；
- 派发 `input/change` 事件；
- 等待可见附件 chip，并用文件名作为成功证据；
- 返回原始文件、暂存文件、哈希、清理和证据。

它的 `src/browser-lock.mjs` 提供跨进程 Profile 锁、心跳、PID 存活检查、超时和 stale lock reclaim。这个锁的设计也适合我们当前的“单 Chrome Profile 单活动任务”调度。

结论：可以复用设计和部分 MIT 代码，但需要把 Node/CDP 客户端适配为 Python Playwright/CDP；不能直接把整个 Pro Line 插件嵌入 MCP。

### 2. `Wangnov/chatgpt-skill`：最适合复用操作协议

它不是运行时库，而是 Codex Chrome Extension 的操作手册。其 Codex runtime 已实测：

```text
添加文件等
→ 添加照片和文件
→ 等待 filechooser
→ chooser.setFiles(绝对路径)
→ 等待附件 chip 和“移除文件”信号
→ 再发送 prompt
```

它明确要求先建立 `filechooser` 监听，再走可见菜单；不要直接点击隐藏 `input[type=file]`。它还记录了上传后验证文件 chip、保留 conversation URL 和 Deep Research 关联流程。

结论：复用它的 UI 语义和失败处理；在我们的 CDP Worker 中优先尝试 Playwright `expect_file_chooser` + 可见菜单。如果正式 Chrome/CDP 下没有可靠的 file chooser 事件，再使用 Pro Line 的 `#upload-files` CDP 适配器作为受控 fallback。

### 3. `dev-newb/chatgpt-clipboard-upload`：算法有价值，运行时不能直接复用

它把文件字节写入 Codex 内置浏览器 clipboard，再粘贴到 ChatGPT composer；由于 ChatGPT 会生成 UUID 文件名，它会逐文件上传并生成“生成名 → 原始 basename”manifest，也支持 ZIP fallback。

但它的 Skill 明确限制为 Codex in-app browser，并明确禁止 Chrome、Edge、CDP、Computer Use 和外部浏览器。我们的 Worker 使用正式 Chrome + CDP，所以不能直接安装或调用这个 Skill 的运行时。

结论：只复用两个思想：逐文件上传避免文件名映射混乱，以及 UUID 文件名 manifest；不复制 clipboard 适配代码。

### 4. `DeliciousBuding/codex-browser-bridge`：可作为另一条桥接底座

它提供 `codex_file_input` MCP 工具，通过 `DOM.setFileInputFiles` 上传；要求绝对路径、`CODEX_BRIDGE_UPLOAD_BASE`、路径在允许目录内、每个文件不超过 10 MB。它还有自己的 Chrome/Edge Codex bridge、tab ownership 和安全 profile。

结论：如果以后决定切换到官方 Codex Desktop browser bridge，可以直接把它作为上传底座；在当前已运行的本地 Python/CDP Worker 中引入它会重复维护浏览器连接和权限体系，暂不直接接入。

### 5. `chrisliu298/chatgpt`：模型选择和轮询参考，明确不支持上传

它的 Codex 方案依赖 `chrome-devtools-mcp` 和现有 Chrome，适合模型选择、发送、轮询和抓取文本；README 明确限制为 text-only prompts，文件和图片上传不支持。

结论：不复用上传；可参考其简单的模型/轮询交互，但当前 Worker 已覆盖更完整的异步任务语义。

### 6. `chatgpt-pro-workforce`：调度/审计参考，范围过重

它有 worker ownership、SQLite ledger、并发限制、文件哈希、artifact storage、恢复和 dashboard。它面向多 Pro worker 编排，默认并发模型也不同于我们的单 Profile 单槽位约束。

结论：只参考状态、ownership、artifact receipt 和恢复字段，不引入整个 workforce；会把当前轻量 MCP 变成另一套编排系统。

### 7. `codex-chatgpt-web`：模型桥接方向不匹配

它把 ChatGPT Web 注册成 Codex 原生模型，并有上传/模型桥接相关实现。我们的目标是保留 ChatGPT Web 的产品工作流、Projects、Deep Research 和独立会话，因此不采用它的 native-model backend 架构。

## 复用结论

最终应采用组合方案：

```text
Pro Line
  ├─ CDP 上传、文件校验、SHA-256、证据和 Profile lock 思路
  └─ receipt / staging / safe cleanup

Wangnov Skill
  ├─ 可见 ChatGPT 菜单和 filechooser 操作顺序
  └─ chip、移除附件、发送前验证

当前 chatgpt-oracle
  ├─ Python MCP
  ├─ SQLite 单槽位调度
  ├─ Streamable HTTP / FRP
  └─ quality=extra_high/pro 和异步 Deep Research
```

不建议直接安装或合并整个社区仓库：它们的 Host、浏览器桥接、状态存储和权限边界不同，直接合并会引入重复 Profile、第二套 CDP 控制权或不兼容的上传路径。

## 后续实现顺序

1. 把 Pro Line 的文件描述、SHA-256、receipt 和清理思路移植为内存上传模块。
2. 在当前 Worker 中增加 `POST /uploads`、短期 `upload_id` 和任务 `upload_ids`；不传本机路径、不做服务端敏感内容拒绝。
3. 先实现可见菜单 + `expect_file_chooser` 上传；失败时使用 Playwright/CDP 协议的内存 `set_input_files` fallback，不落本机路径。
4. 上传后返回 attachment evidence 和 original/generated filename map。
5. 在发送前把附件信息写入 task receipt；失败时只清理本次生成的附件。
6. 为单槽位调度增加上传阶段状态：`UPLOADING`、`ATTACHMENTS_VERIFIED`、`SEND_PENDING`。
7. 用一个无敏感内容的 smoke 文件验证真实上传，再验证上传文件后普通咨询和 Deep Research。

## Sources

- Wangnov Codex Chrome runtime：<https://github.com/Wangnov/chatgpt-skill/blob/main/codex-chatgpt-skill/references/codex-chrome-runtime.md>
- ChatGPT Pro Line upload：<https://github.com/pauljunsukhan/codex-chatgpt-pro-plugin/blob/main/src/chatgpt-upload.mjs>
- ChatGPT Pro Line browser lock：<https://github.com/pauljunsukhan/codex-chatgpt-pro-plugin/blob/main/src/browser-lock.mjs>
- Clipboard Upload Skill：<https://github.com/dev-newb/chatgpt-clipboard-upload/blob/main/SKILL.md>
- Codex Browser Bridge file input：<https://github.com/DeliciousBuding/codex-browser-bridge/blob/main/skills/codex-browser/SKILL.md>
- 本地调研记录：[ChatGPT-调研 ChatGPT Skill-20260915-0313.md](<D:/Project/NEKO/gpt explorer/ChatGPT-调研 ChatGPT Skill-20260915-0313.md>)
