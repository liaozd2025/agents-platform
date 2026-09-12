# 外部智能体连接知识库

Yuxi Knowledge MCP 为外部智能体提供只读知识库工具。客户端使用 Streamable HTTP，经浏览器登录和确认后取得 30 天有效的专用授权；无需安装 yuxi-cli。

## 配置连接

本机开发默认地址为 `http://localhost:5173/api/mcp`。在 Codex 中添加：

```sh
codex mcp add yuxi-knowledge --url http://localhost:5173/api/mcp --oauth-client-registration dcr
codex mcp login yuxi-knowledge --scopes knowledge:read
```

添加时如果已完成登录，无需重复执行 login。浏览器展示客户端名称、回调地址与只读范围；用户确认后返回客户端。重新加载会话发现工具，调用 `list_kbs` 查看当前账号可用的库。Workbody 等客户端需支持远程 HTTP MCP、OAuth 授权码、PKCE S256 与动态客户端注册，具体产品版本需实测。

## 权限与工具

可访问范围由当前登录用户的 `knowledge_base:read` 或 `knowledge_base:manage` 功能权限，以及知识库所有者/共享范围决定。MCP 每次调用重新核验 PostgreSQL 当前权限；模型不提交 uid，Skill 或客户端中的库 ID 不授予权限，也不套用平台 Agent 的 `context.knowledges`。

工具为 `list_kbs`、`query_kb`、`search_file`、`open_kb_document`、`find_kb_document`。它们支持发现、检索、文件名查找、原文窗口和字面术语定位；不提供写入、删除、上传或原始附件下载。Word 原始模板与报告由外部宿主处理。

未登录返回 401 并提供 OAuth 发现地址。账号或功能权限失效会使后续请求被拒绝；库不可见时工具报告不存在或无权访问。授权到期重新登录；客户端 logout 可调用标准撤销接口。MCP 凭据不能用来调用普通平台 API。

## 部署边界

生产默认不注册 MCP。通过服务端环境变量 `YUXI_KNOWLEDGE_MCP_URL=https://knowledge.example.com/api/mcp` 配置固定入口后，在正常部署流程中重启 API。Compose 已通过 env_file 读取该变量。LITE 始终不注册知识库 MCP。正式部署使用可信 HTTPS，不关闭证书验证。

内网开发时，设置 `YUXI_ENV=development` 并显式配置 `YUXI_KNOWLEDGE_MCP_URL=http://<内网IP>:5173/api/mcp`，客户端连接同一地址。仅 RFC1918 私有 IP 和本机地址允许开发 HTTP；HTTP 不加密，只用于可信内网测试，生产仍拒绝 HTTP。OAuth 客户端的本机回调由客户端生成，不改为服务端 IP。

经部署负责人明确接受明文风险的内网测试，可以同时配置 `YUXI_KNOWLEDGE_MCP_ALLOW_PRIVATE_HTTP=true` 与私有 IP 的 `YUXI_KNOWLEDGE_MCP_URL`。此开关默认关闭，只放行显式配置的 RFC1918 IP，不接受公网 IP、域名或生产 localhost HTTP，不改变平台的 production 模式与用户权限。私有 IP 校验不限制网络来源，部署方仍需确保入口仅内网可达。切换 HTTPS 时关闭此例外并重新授权。

反向代理需转发 `/api/` 以及 `/.well-known/oauth-`。仓库的 Vite 开发代理与 Nginx 模板包含对应规则；实例域名不可从不可信 Host 头推导。切换地址后客户端重新注册、授权并发现知识库，不复制旧地址的凭据。

Redis 保存带 TTL 的客户端登记、十分钟待确认请求、两分钟授权码与 30 天授权。Token 从签发起固定有效 2592000 秒，使用不会续期；修改有效期只影响新签发的 Token，已有 Token 不自动延长。授权码原子交换并防止重复使用；网络中断导致交换结果丢失时需重新登录。Redis 清空会使客户端重新注册和登录，不能降级匿名访问。PostgreSQL 持续拥有用户、角色与知识库权限。当前不提供 refresh token 或持久授权管理页面。

## 验证入口

权限与协议回归位于 `backend/test/integration/api/test_knowledge_mcp.py`，通过真实 HTTP、PostgreSQL 和 Redis 验证 OAuth、PKCE、用户隔离、权限变更与撤销。输入边界位于 `backend/test/unit/services/test_knowledge_mcp_service.py`。真实客户端登录与业务报告验收仍需另外执行，不能由单元测试代替。

平台连接第三方 MCP 的配置见 [MCP 集成](./mcp-integration.md)。
