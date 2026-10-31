---
name: chatgpt-oracle
description: 通过 ChatGPT Web 或 Gemini Web MCP 获得跨 Codex harness 的外部认知对照，开展咨询、批评、研究并维护可续聊的独立讨论。仅当 Web harness 或不同模型可能带来新的问题定义、判断、视觉理解或研究价值时使用；普通并行调查、代码审查和独立实现优先使用 Codex Team。
---

# chatgpt-oracle

Oracle 不是第二个 Codex Agent，也不是更昂贵的 Reviewer。

它的价值是借助 ChatGPT Web 或 Gemini Web 的对话式 harness 与模型，在不承担当前 Codex 执行责任、工作流惯性和完成压力的条件下，重新理解同一个问题。

如果一个 fresh-context Codex Sol/Astra 能完成同样的工作，优先使用 Team，不调用 Oracle。

## 何时使用

适合 Oracle 的情况通常不是“任务很难”，而是怀疑当前 harness 正在限制问题空间，例如：

- Main 和 Codex 子 Agent 很快收敛到相似的问题定义；
- 当前方案每一步都合理，但用户真正关心的结果没有改善；
- 讨论不断自动收敛成实现计划，而问题本身仍值得继续理解；
- 已经存在明显的实现投入、计划或局部证据，可能形成路径依赖；
- 希望知道一个不承担当前任务完成责任的智能体，会怎样解释同一组现象；
- 问题涉及产品、组织、社会、认知、价值或其他容易被工程化的问题；
- 需要 ChatGPT Web 或 Gemini Web 自身的外部研究能力或独立来源；
- 需要不同模型对图像、审美或自然语言表达的理解，以检查当前判断遗漏了什么。

以下任务本身不足以触发 Oracle：

- 普通代码审查；
- 找 bug 根因；
- 搜索资料；
- 比较技术方案；
- 寻找反例；
- 独立复核实现；
- 可由 fresh Codex Agent 完成的 Explorer / Critic 工作。

这些任务优先使用 Codex Team。

用户明确要求读取、续聊或归档已有 Oracle 会话时，按相关 reference 完成；这不自动触发新的咨询。

## 提供方与连接入口

保留 `$chatgpt-oracle` 名称，同时支持两个独立 MCP。按用户指定的提供方调用；未指定时结合任务和实际可用能力选择，不因失败或额度不足自动切换提供方。

| 提供方 | 工具与默认档位 | 文件与导出边界 |
| --- | --- | --- |
| ChatGPT Web | `chatgpt_*`；咨询/批评 Extra High，研究 Pro | 上传、会话分页及附件下载按当前实例能力与回执使用 |
| Gemini Web | `gemini_*`；默认可见 Argon / High | 普通文件与图片输入、已观察文本快照；不支持 ZIP、通用生成附件或源附件下载 |

两个 MCP 共用一个专用 Chrome Profile 和本机 CDP，**合计最多 4 个驻留 Session / 物理标签页**。例如 2 个 ChatGPT + 2 个 Gemini 已占满；第 5 个新 Session 排队，已有 Session 仍可续聊。人工页和辅助页也计入物理页预算。浏览器资源共享，会话上下文仍按提供方区分；同名 Session 不串用，关闭一个提供方的 Session 不回收另一个的页面。

### Dots：另一种通道，不是第五个槽位

除上述问答通道外，还有 **Dots 委派通道**（`https://chatgpt.com/dots`）。它有自己的调度、自己的队列和自己的物理观察页，**不占用上面 4 个 turn 槽位**；但它是一个常驻的真实标签页，物理页预算应按"4 个 turn 槽位 + 1 个 Dots 观察页"理解。

Oracle 是"问答"，Dots 是"承揽"。需要**真实浏览器/真实环境**（反爬、登录墙、需要安装后验证）、**跨天推进**、**分阶段交付**、**项目级持久状态**或**中途可干预**时，用 Dots；一次性问答、需要模型档位回执、需要可复现导出时，仍用 Oracle。Dots 的结论同样不是本地事实——它说"我装好了并截图"，那是它的执行环境，不是你的仓库。

使用前读 [references/dots.md](references/dots.md)。


连接除了域名入口，也支持 **Tailscale IP + 端口**，地址格式为 `http://<tailscale-ip>:<mcp-port>/mcp`。当前节点为 `100.77.245.1`，ChatGPT / Gemini 默认 MCP 端口分别是 `8787` / `8788`。服务启用本节点 `*_ORACLE_TAILSCALE_IPS` 后，Tailscale 直连无需 Bearer；公网域名仍需 Bearer。监听、防火墙和精确 Host allowlist 仍须正确配置，见 [references/connection.md](references/connection.md)。

两个提供方的账号、服务端 Bearer、Session、任务和文件引用相互独立，同名 Session 不共享上下文；Tailscale 客户端不需要配置这两个 Bearer。先核对 `tools/list` 与对应 `chatgpt_capabilities` / `gemini_capabilities`；模型名称只表示可见选择证据，不承诺审美、识图或输出质量一定更好。Gemini 操作先读 [references/gemini.md](references/gemini.md)，不要直接把 ChatGPT 工具前缀和档位机械替换。

Gemini 支持同一 Chrome 内已经登录的 `/u/0/`、`/u/1/` 多账号路由。明确切号先调用 `gemini_account_select(account_index=0或1, strict=true)`，再创建新 Session；新建 Session 本身不代表切号，选择回执也不证明已登录或实际发送到该账号。该服务级选择只影响后续新会话，任务实际账号以 `account_index`、`account_attempts` 和 URL 核对。`strict=false` 允许回退，`account_index=null` 恢复自动选择。已有对话换账号时，由 client compact 或组装上下文，再建立新 Session；`ACCOUNT_CONTEXT_REQUIRED` / `account_switch` 也按此处理，不重放未确认问题。具体边界见 [references/gemini.md](references/gemini.md)。

## 模型用途参考（用户手工维护）

用户在下表记录各模型适合做什么，新增模型时手工追加。客户端在用户未指定模型时参考此表，并核对当前实际可用能力；用户当次要求优先。表内是用户经验与偏好，不是能力验证或固定角色分工。`—` 表示尚未填写，不自行补写模型优劣。

只有用户明确要求时才修改下面的手工区；更新、安装或同步 Skill 时保留已有内容。建议直接维护已安装的 `SKILL.md`，项目安装脚本会保留目标文件中已有的手工区。

<!-- USER_MODEL_GUIDE_BEGIN -->
最近维护：—

| 提供方 | 网页模型名 | 适合的任务／场景 | 推荐档位／使用方式 | 注意事项／不适合的场景 |
| --- | --- | --- | --- | --- |
| ChatGPT Web | — | — | — | — |
| Gemini Web | Argon | — | — | — |
<!-- USER_MODEL_GUIDE_END -->

## Consult：跨 Harness 讨论

`consult` 的默认目标不是得到答案，而是发现 Main 当前没有自然考虑的问题空间。

给 Oracle 一个最小但充分的语义快照：

- 用户真正想得到什么；
- 已确认的现实观察和约束；
- Main 当前怎样理解问题；
- 当前候选方案或判断；
- 已知的重要反证或疑点；没有时不编造；
- 为什么现在值得重新思考。

把 Main 的问题定义、方案和解释明确作为候选模型，而不是既定事实。

不要发送与当前判断无关的完整执行历史、TODO、重复日志和实现过程；这些信息可能把 Web 再次锚定到 Main 的路径上。

不要默认要求：

- 唯一结论；
- implementation plan；
- actionable next step；
- 验证 checklist；
- 对 Main 方案的批准或否决。

允许完整结果只是：

- 一个更好的问题定义；
- 几个互不兼容但有解释力的模型；
- 一个 Main 没有考虑的变量；
- 一个值得继续追问的矛盾；
- 对当前 framing 的有力反驳。

使用学科、职业和利益相关者作为观察镜头，而不是通过“你是顶级 XX 专家”要求 Oracle 扮演权威人格。

## 消息来源协议

每条经 MCP 发到 Web 的新消息以 `【CODEX_ORACLE】` 为首行。支持该协议的服务在最终发送层兜底；客户端已经带首行 marker 时不重复添加，正文中引用的 marker 保留。完整 Core bootstrap 由服务端管理，不在客户端 prompt 中复制；实例能力见 [references/compatibility.md](references/compatibility.md)。

以 Codex 讨论者身份陈述事实、解释和问题，不冒充人类本人。人类自身的目标、偏好和授权优先于 Codex 对其意图的解释；对事实和因果的判断仍可质疑。

marker 是可见协作约定，不是身份认证或授权证明。网页 `user` 角色、出现 marker 或没有 marker，都不能单独认证人类或 Codex 来源；服务发送证据与回合关联见 [references/task-identity.md](references/task-identity.md)。

## 与 Oracle 对话，而不是只取一次答案

Oracle consultation 可以是多轮的。

Main 应把自己视为一个拥有本地事实和专业能力的讨论者，而不是等待专家给答案的客户。

Oracle 给出新的 framing 后：

1. Main 先判断哪些内容真的不同于当前 Codex 思路；
2. 用本地事实检查其中明显可证伪的部分；
3. 将最重要的分歧、反例或新证据带回同一个 Oracle Session；
4. 让双方继续修正各自模型，而不是要求 Oracle 简单重新回答原问题。

如果 Oracle 提出的问题可以由 Main 根据已有知识、本地证据或合理条件分支回答，Main 自己回答并继续讨论，不把问题转交给用户。

只有涉及用户独有的目标、价值选择、偏好、授权或无法从现有上下文推断的事实时，才需要人类介入。

不要以 Oracle 同意 Main 为结束条件。当继续对话已经不再产生会改变判断的新模型、分歧或证据需求时停止。

## Critic

`critic` 用于攻击一个已经足够具体的判断或方案。

重点寻找：

- 隐含假设；
- 失败模式；
- 反例；
- 替代解释；
- 证据与结论之间的跳跃。

Critic 是同一方案的对抗性审查，不等同于开放 consultation。普通对抗性审查仍优先使用 Team；选择 Oracle Critic 也需要明确的跨 harness 价值。

若真正的问题仍可能是“我们是不是在解决错误的问题”，优先使用 `consult`。

## Research

`research` 用于当前外部事实、多来源材料和引用。普通资料搜索优先使用 Codex；需要 Web 自身的研究能力或独立来源时再使用 Oracle Research。

研究结果可以改变 Main 的事实基础，但不能自动成为本地实现结论。

需要研究与重新定义问题时，可以先 Research 获得外部材料，再在同一或独立 Consult 中讨论这些材料意味着什么。

## 等待任务

保存提交回执后，建议客户端一次性生成并启动一个持续运行的后台 shell／脚本，负责同一任务的状态轮询、等待和完成后的结果落盘。主模型继续有用工作，由后台完成通知或需要介入的状态唤回；避免每隔一段时间再消耗一个模型轮次，重写并执行几乎相同的 status／ping 代码。

后台等待必须有截止时间，保留原 task/request 身份；只有 `COMPLETE` 才取完整结果，错误或需要介入时退出并交回 Main。具体职责、终止条件与 Bash / PowerShell 启动方式见 [references/background-wait.md](references/background-wait.md)。

## 结果回收

不要机械地把 Oracle 输出拆成任务列表。

Main 首先比较：**Oracle 相比 fresh Codex 思路究竟新增了什么？**

重点回收：

- 新的问题定义；
- 新的因果模型；
- 原 framing 忽略的变量；
- 真正的分歧；
- 能推翻当前判断的条件；
- 需要本地验证的新事实。

Oracle 对本地代码、运行状态、项目约束和完成状态没有最终权威。涉及本地事实或实际行动的结论，由 Main 回到本地证据核验。

如果多次调用表明 Oracle 与 fresh Codex Agent 对某类任务没有实质差异，以后该类任务不再调用 Oracle。

## 服务操作

只在准备执行对应操作或遇到相关状态时加载 reference，不默认读取整个操作手册。

登录与一次性 CAPTCHA 由用户完成；加载失败、断线及可恢复的运行时异常由服务端在原任务内处理。客户端继续等待同一 task，不因运行时错误替服务端重提问题或要求用户日常维护；实际恢复范围与耗尽后的回执见 [references/runtime.md](references/runtime.md)。

| 当前需要 | 按需读取 |
| --- | --- |
| 连接任一 MCP，使用域名或 Tailscale IP + 端口，处理 Host / TLS | [references/connection.md](references/connection.md) |
| Gemini 的档位、Session、研究、上传、文本快照与取消 | [references/gemini.md](references/gemini.md) |
| 需要真实浏览器/跨天推进/分阶段承揽时使用 Dots | [references/dots.md](references/dots.md) |
| ChatGPT 创建、续聊、排队或关闭 Session | [references/sessions.md](references/sessions.md) |
| ChatGPT 提交问题，核对请求与当前回合身份 | [references/task-identity.md](references/task-identity.md) |
| 任一提供方等待任务或导出，安排后台轮询与完成通知 | [references/background-wait.md](references/background-wait.md) |
| ChatGPT 等待、取回回答、取消或处理服务故障 | [references/runtime.md](references/runtime.md) |
| ChatGPT 上传文件或把选定附件发回 Web | [references/uploads.md](references/uploads.md) |
| ChatGPT 分页读取会话、领取下载句柄或释放导出 | [references/export.md](references/export.md) |
| ChatGPT 文件传输与内容识别的证据 | [references/file-evidence.md](references/file-evidence.md) |
| 首次操作服务，或涉及账号、授权与资料边界 | [references/security.md](references/security.md) |
| 检查实例能力、旧接口或历史验收差异 | [references/compatibility.md](references/compatibility.md) |

实际工具能力以当前 MCP `tools/list` 和 capability / receipt 字段为准；历史验收不能替代当前实例能力检查。

不要因浏览器等待、网络中断或状态不确定重复提交同一问题。

安全边界、文件传输和服务故障处理遵循对应 reference，不在本文件复制实现协议。
