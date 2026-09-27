# ChatGPT Oracle 模型选择扩展计划

## 目标

让 MCP 任务可以明确选择 ChatGPT 的能力档位：

- 普通问题默认使用 `extra_high`；
- 需要 Pro 级别调研或复杂讨论时使用 `pro`；
- 保留显式模型和能力滑块控制；
- 当前账号或当前 UI 没有 Pro 时明确报告不可用，不静默降级。

## 已确认的本地证据

当前 `chatgpt_submit` 已经接受 `model` 字段，但浏览器层的 `_select_visible_model()` 只是按可见文本点击，不能设置右侧能力滑块，也不会返回最终解析出的模型/能力。

当前 ChatGPT 页面实际存在：

- `data-testid="composer-intelligence-picker-content"`；
- `data-model-reasoning-effort-slider`；
- `role="slider"`，`aria-valuemin="0"`、`aria-valuemax="4"`；
- 独立的 `role="menuitemradio"` 模型列表；
- 当前这次账号页面显示“最新 / GPT-5.6 Sol / GPT-5.5”，未显示 Pro。

因此实现不能依赖固定坐标、固定模型列表或把滑块值直接猜成某个模型名称。

## 计划阶段

### P0：定义兼容的 MCP 数据模型

扩展 `chatgpt_submit`：

```text
quality: "extra_high" | "pro" | "auto"
model: string | null              # 可选的可见模型名/别名
reasoning_effort: string | null   # 可选的可见能力名
allow_downgrade: boolean = false
```

兼容旧客户端继续接受 `model`。新增字段优先级为：显式 `model` / `reasoning_effort`，其次 `quality`，最后按任务类型使用默认值。

默认策略：

| 任务类型 | 默认档位 |
|---|---|
| `consult` | `extra_high` |
| `critic` | `extra_high` |
| `research` | `pro` |

`chatgpt_continue` 默认沿用父任务的解析结果，也允许显式覆盖。

### P1：增加能力发现工具和结果字段

增加 `chatgpt_capabilities`，返回当前页面实际可用的：

- 模型名称和可见别名；
- 当前能力滑块标签、范围和当前值；
- 是否检测到 `pro`；
- 页面 URL 和 Profile 状态。

任务元数据增加：

```text
requested_quality
requested_model
requested_reasoning_effort
resolved_model
resolved_reasoning_effort
selection_source = explicit | quality_alias | default
selection_verified_at
```

`chatgpt_status` 和 `chatgpt_result` 都返回解析结果，便于 Codex 知道实际使用了什么。

### P2：实现 DOM 优先的选择器

浏览器层在发送消息前执行：

1. 打开当前模型/能力菜单；
2. 读取可见菜单项和 `role=slider` 的当前状态；
3. 对 `extra_high`：选择可见的 Extra High/最高能力标签，或通过键盘调整滑块后读取可见确认文本；
4. 对 `pro`：进入模型选择视图，按可见文本和别名匹配 Pro；
5. 重新读取 UI 验证选择结果；
6. 找不到目标时返回结构化 `MODEL_UNAVAILABLE`，包含实际可用选项；
7. 只有显式 `allow_downgrade=true` 时才允许回退，并把回退写入结果元数据。

实现只使用可见文本、ARIA、`data-testid` 和 `role`，不依赖 CSS 位置、内部模型 ID 或 API 请求。

### P3：更新 Skill 的调用策略

Skill 中明确：

- 一般咨询不传 Pro，使用 `quality="extra_high"`；
- 复杂架构讨论或用户明确要求 Pro 时传 `quality="pro"`；
- Deep Research 默认传 `quality="pro"`；
- 提交前先调用 `chatgpt_capabilities`，若 Pro 不可用，报告可用档位并停止静默替换；
- 结果中必须向 Codex 回报实际解析的模型和能力。

### P4：测试和真实验收

自动化测试：

- quality 默认值和别名解析；
- 显式参数覆盖默认值；
- 模型不可用时返回结构化错误；
- 任务元数据保存 requested/resolved 字段；
- 继续任务继承或覆盖选择；
- 滑块控件值和模型列表的 DOM fixture 选择测试。

真实浏览器测试：

1. `chatgpt_capabilities` 读取当前账号的实际菜单；
2. 提交普通咨询，验证最终为 Extra High；
3. 提交 Pro 咨询，验证最终为 Pro；
4. 提交 Deep Research，验证任务启动前已经选择 Pro；
5. 在当前账号没有 Pro 的情况下，验证返回 `MODEL_UNAVAILABLE`，不自动改用 Extra High；
6. 验证 MCP 返回 resolved model/effort 和对话 URL。

跨设备测试继续复用现有 HTTP MCP/Bearer/FRP 验收，不新增浏览器暴露面。

## 交付物

- 更新后的 `chatgpt_submit` / `chatgpt_continue` schema；
- `chatgpt_capabilities` 工具；
- 模型/能力选择器和结构化不可用状态；
- 持久化任务元数据；
- 更新后的 Skill、README 和测试；
- 一份真实账号能力发现结果和普通/Pro/Deep Research 验收记录。

## 重要验收边界

Pro 是否可用由当前 ChatGPT 账号和页面实时决定。实现会准确报告页面事实，不会把 Extra High 冒充成 Pro，也不会把当前页面没有显示的 Pro 当作已支持。

## 当前实现验证

当前账号的实时能力发现结果为：

```text
0 即时
1 中
2 高
3 极高（Extra High）
4 Pro
```

已验证：

- `chatgpt_capabilities` 能通过 Streamable HTTP 返回模型列表、滑块范围和质量别名；
- 显式 `quality="extra_high"` 的真实任务返回 `resolved_reasoning_effort="极高"`；
- 显式 `quality="pro"` 的真实任务返回 `resolved_reasoning_effort="Pro"`；
- 不传 `quality` 的普通咨询返回 `quality="auto"`、`selection_source="default"`、`resolved_reasoning_effort="极高"`；
- 自动化回归测试为 10 项通过；
- Pro 不可用时使用 `MODEL_UNAVAILABLE`，不会静默降级。
