# 按用户权限提供远程知识库 MCP

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/knowledge_mcp_service.py

## 问题

外部通用智能体需要无需安装 CLI 即可登录并查询用户有权读取的知识库。

## 决策

使用现有 MCP Python SDK 提供 Streamable HTTP 和 OAuth 授权码加 PKCE，复用平台登录页与后端用户权限。仅提供列表、检索、文件列表、打开、定位五项只读工具。每次调用重新读取 PostgreSQL 用户与资源权限；客户端不传 uid。Redis 保存短期 OAuth 会话，失效即重新登录，不提供长期 refresh token。部署入口必须显式配置，LITE 不注册。

## 替代方案

CLI 需要客户端安装；静态共享 API Key 无法表达各用户权限；直接接受普通平台 Token 会扩大凭据用途。选择专用于 MCP 的短期授权。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 未登录不能调用工具 | 匿名读取 | MCP 入口 | test/integration/api/test_knowledge_mcp.py | 无 Token 请求 | Passed |
| 用户只能读授权库 | 伪造 kb_id | knowledge_mcp_service | 同一真实 HTTP 测试 | 跨用户四项定向操作 | Passed |
| OAuth 绑定客户端与资源 | PKCE/资源错配 | MCP OAuth provider | 同一真实 HTTP 测试 | 错误 verifier/resource | Passed |

执行 `docker compose exec -T api uv run --no-sync --group test pytest test/integration/api/test_knowledge_mcp.py -q`，真实 PostgreSQL、Redis 和 HTTP 测试通过；包含角色权限撤销及授权撤销后恢复角色的负向验证。输入边界、LITE 与 lifespan 相关 unit 17 项通过。Web lint、236 项 unit、Web build、工程信任检查与 61 项 verifier unit、docs build 通过；docs build 存在既有 VitePress/Rolldown 警告。

Codex 已完成元数据发现、DCR 与跳转平台登录；等待人工登录回调超时，真实账号在 Codex 中的取证尚未验收。Workbody、完整药品准入报告与外网未验收。测试数据为临时账号和库，执行后清理，不使用共享管理员凭据。

## 后果

内网部署例外：部署负责人明确要求在现有 production 实例通过私有 IP HTTP 测试。提供默认关闭的 `YUXI_KNOWLEDGE_MCP_ALLOW_PRIVATE_HTTP`，只与显式 RFC1918 入口共同生效；默认生产 HTTPS 策略不变。拒绝将整个实例改为 development 或放行任意 HTTP。密码和 MCP 凭据仍可能被内网窃听，此例外不提供传输保密性或来源 IP 限制，外网切换须配置 HTTPS 并关闭例外。输入边界测试覆盖显式放行、未授权拒绝及公网/域名/生产本机拒绝。

真实客户端登录需要用户浏览器确认。Redis 失效会要求重新登录；授权不扩展 PostgreSQL 权限。外网需 HTTPS、限流及客户端实测，当前不发布外网。

development 允许显式配置的 RFC1918 IP 使用 HTTP；production 仅在上述显式内网例外开启时允许。SDK AuthorizationHandler 校验 PKCE 和回调；资源地址同时作为 issuer。Vite 的 MCP 代理与 Nginx API 代理保留含端口的请求 Host，避免代理改写触发 DNS rebinding 防护；服务端 Host 白名单保持限定。

通过 IP 和 5173 端口执行 `docker compose exec -T -e TEST_BASE_URL=http://<内网IP>:5173 api uv run --no-sync --group test pytest test/integration/api/test_knowledge_mcp.py test/unit/services/test_knowledge_mcp_service.py -q`，15 项通过，覆盖发现、登录、越权拒绝、撤销以及生产 HTTP 拒绝。Codex IP 连接完成动态注册并生成授权地址，人工确认仍待完成。Web lint 与 docs build 通过，docs 存在既有构建警告。

MCP SDK 负责授权请求、PKCE 与 token 交换协议；公开客户端注册及撤销适配修正 SDK 对 refresh_token/client_secret 的默认要求。OAuth 授权码按协议一次使用，交换响应丢失需重新授权；与可幂等重放的长期 API Key 创建接口区分。凭据 30 天过期、无 refresh token；OAuth 状态可丢失，不改变持久用户权限。

2026-09-12，按用户要求将新签发的 MCP Token 固定有效期延长至 30 天。服务层单一常量同时控制 expires_at、Redis TTL 与 OAuth expires_in，授权确认页显示相同天数；旧 Token 不迁移，调用不续期。保留逐次用户权限检查、主动撤销及两分钟授权码。与维持一小时方案相比，减少重复授权，但泄露后可被滥用的时间窗口更长，尤其需要注意内网 HTTP 的明文风险。集成测试以独立期望值 2592000 校验真实响应、Redis 过期时间与 TTL，并保留撤销和越权负向测试。

客户端登记有效期在签发 Token 的同一 Redis 原子操作中延长至 30 天，覆盖最新 Token 的撤销窗口；客户端已过期则拒绝签发。真实 HTTP 回归将登记 TTL 缩短至 60 秒后交换授权码，校验登记 TTL 覆盖 Token，并验证主动撤销。
