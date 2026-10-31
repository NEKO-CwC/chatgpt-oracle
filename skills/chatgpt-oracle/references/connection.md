# MCP 连接

在首次连接、更换入口或排查网络故障时读取。账号与凭据边界见 [security.md](security.md)。

## 域名与 Tailscale IP + 端口

HTTP MCP 可以使用域名入口，也可以通过受限 Tailscale 网络直接访问节点的 IP 和 MCP 端口。当前节点 Tailscale IPv4 为 `100.77.245.1`；地址或端口变更时以实际部署配置为准。

| 提供方 | 访问方式 | MCP 地址 | 状态与条件 |
| --- | --- | --- | --- |
| ChatGPT | 公网域名 / FRP | `https://chatgpt-oracle.frp-service.neko-dashboard.com:32751/mcp` | 已配置过；2026-10-03 曾有 TLS/连接失败，当前可用性需检查 |
| ChatGPT | Tailscale IP + 端口 | `http://100.77.245.1:8787/mcp` | 2026-10-14 远程验证 health、MCP initialize 与 28 工具发现，无需 Bearer，使用默认 IP Host |
| Gemini | 本机 loopback | `http://127.0.0.1:8788/mcp` | 当前监听所有 IPv4；loopback 仍需服务端 Bearer，本机也可使用 Tailscale IP 免 Bearer |
| Gemini | Tailscale IP + 端口 | `http://100.77.245.1:8788/mcp` | 2026-10-14 远程验证 health、MCP initialize 与 26 工具发现，无需 Bearer，使用默认 IP Host |
| Gemini | 公网域名 / FRP | 按实际部署的 HTTPS 域名填写 | 尚未配置或验证，不复用 ChatGPT 域名指向另一个提供方 |

当前 ChatGPT Broker 监听 `100.77.245.1:8787`，Gemini 监听 `0.0.0.0:8788`。本机 `127.0.0.1:8787` 被 Kiro 占用，保留该进程；连接 ChatGPT Broker 使用上述 Tailscale 地址，不能将 Kiro 的 loopback 端口当成 Broker。两个 Broker 已配置隐藏的 Windows 登录启动任务；防火墙仅放行 Tailscale 网卡、该节点 IP、tailnet IPv4 来源和这两个 TCP 端口。2026-10-14 的远程验证来自 tailnet 主机 deepseek1；不表示公网 FRP 已恢复，也不证明后续永远在线。

2026-10-15 起，两者共用已登录的原生 Profile `~/.agent-browser/gemini-native-profile` 与本机 CDP `127.0.0.1:9333`，合计四个驻留 Session / 标签页；旧 ChatGPT CDP 9222 已停用。配置由共享 `ORACLE_SHARED_BROWSER`、`ORACLE_BROWSER_PROFILE_DIR`、`ORACLE_BROWSER_CDP_URL` 管理，默认使用共享模式；原先的提供方独立 Profile/CDP 选项只用于显式关闭共享模式的兼容配置。远端继续仅访问 MCP 端口。

2026-10-29 起 `start-mcp.ps1` 在共享模式下先执行 `wait-browser-cdp.ps1`：轮询 CDP `/json/version`，端口未监听或不可调试时最多等待 `StartupWaitSeconds` 再初始化 Broker（`CHATGPT_ORACLE_SKIP_CDP_WAIT=1` 可跳过）。原因是同一 profile 被一个不带 `--remote-debugging-port` 的桌面 Chrome 持有 `lockfile` 时，Broker 无法附加、也无法另起第二个实例；此前它只在第一个请求上报 `BROWSER_ERROR`。用 `scripts/setup-browser.ps1` 启动共享 Chrome，或先关闭占用 profile 的桌面 Chrome。

## Tailscale 连接条件

- 客户端与服务节点位于可互通的 tailnet，ACL 和主机防火墙允许对应 MCP 端口；为目标 Tailscale IP 绕过环境代理。
- Broker 必须监听 Tailscale IP 或 `0.0.0.0`，仅监听 `127.0.0.1` 时远端无法连接。`0.0.0.0` 是监听配置，不是客户端目标。
- 配置 `CHATGPT_ORACLE_TAILSCALE_IPS` / `GEMINI_ORACLE_TAILSCALE_IPS` 为本节点的精确 Tailscale IP，例如 `100.77.245.1`。启用后，实际本机目标地址命中配置，且实际对端属于 Tailscale 地址范围时，MCP 与文件传输都无需 Bearer；仅设置 Host、X-Forwarded-For 或 Forwarded 不会获得豁免。服务禁用代理头重写，使用 socket 两端地址判断。
- 公网 / FRP 和 loopback 连接继续使用各自 `*_ORACLE_BEARER_TOKEN`。Tailscale 客户端无需 Gemini Bearer 文件，也无需传 Authorization。未配置 Bearer 的 Tailscale 专用服务拒绝其他连接，不会把所有网卡变成匿名入口；健康检查仍是无凭据诊断接口。
- `*_ORACLE_TAILSCALE_IPS` 会自动把精确 `IP:监听端口` 加入 MCP Host allowlist。额外域名继续使用 `CHATGPT_ORACLE_ALLOWED_HOSTS` / `GEMINI_ORACLE_ALLOWED_HOSTS`，包括准确端口，不关闭 Host 校验。

ChatGPT 历史 Tailscale 路径沿用域名 Host 与 Bearer，IP Host 曾返回 421；这是旧配置的历史证据。启用上述新逻辑后直接使用 IP URL 的默认 Host 即可，无需沿用域名 Host 或 Bearer。仍返回 421 时检查所连实例是否已升级、是否配置本节点 Tailscale IP 与当前端口。

Gemini 的配置示例（供部署时使用，本 Skill 不执行重启）：

```powershell
.\scripts\start-gemini-mcp.ps1 -Transport http -Port 8788 -ListenHost '0.0.0.0' -TailscaleIps '100.77.245.1' -PublicBaseUrl 'http://100.77.245.1:8788'
```

该示例在项目目录执行，仅供已获授权的部署；不因读取 Skill 自动改监听、凭据或防火墙。`-PublicBaseUrl` 设置文件传输的客户端可达 origin（不加 `/mcp`），避免回执中的上传 URL 指向远端客户端自己的 127.0.0.1。HTTP Broker 与 stdio 的 loopback 内部转发仍使用服务端独立 Bearer，Tailscale 客户端不需要取得它。`/internal/exports` 不向远端开放。Chrome CDP 保持本机，远程只访问 MCP 端口。公网域名入口继续使用 HTTPS；Tailscale HTTP 只用于受限 tailnet。

## 核对顺序

更换入口时核对同一 origin 的 `/healthz`、MCP initialize 和 `tools/list`，确认所连服务与 `chatgpt_` / `gemini_` 工具前缀一致，再用对应 `*_capabilities(scope="service")` 检查需要的能力。ChatGPT 见 [compatibility.md](compatibility.md)，Gemini 见 [gemini.md](gemini.md)。健康检查通过只证明连接层的相应检查通过，不证明网页就绪、任务成功或已登录。

FRP/TLS、代理、Host 421 与网页选择器、档位或发送错误分别排查。保留已有连接配置中的凭据，不将 Bearer 值写进 prompt、日志或交付文件。连接中断后保存原任务身份并查询原任务；不要重复发送问题。提供方之间不能混用任务、Session 或文件引用。
