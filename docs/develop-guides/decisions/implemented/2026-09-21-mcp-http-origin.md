# MCP HTTP 请求限制在配置的源站

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/agents/mcp/service.py

## 问题

SSE 与 Streamable HTTP 默认跟随重定向。HTTP 客户端会移除部分标准认证头，但自定义密钥头仍可被转发给另一主机。[扩展执行调查](https://github.com/liaozd2025/agents-platform/issues/97#issuecomment-5759489684)已通过本地真实 MCP 协议复现。

## 决策

在 MCP 客户端统一装配 HTTP client factory，为每条连接绑定配置 URL 的协议、主机和端口。每个实际 HTTP 请求（含重定向和后续工具调用）发送前检查该 origin，不一致就拒绝请求。同源相对/绝对跳转继续工作；内置 stdio 不受影响。使用 SDK 既有客户端工厂保留认证、超时和生命周期默认值；工厂仅放入运行时副本，不进入配置哈希或数据库。

## 替代方案

仅剥离 Authorization 无法覆盖任意自定义密钥头；维护秘密头名单也无法穷举。禁止全部重定向会额外破坏正常的同源路径调整。允许跨源但删除自定义头仍可能发送带秘密的请求体，因此选择在发送前拒绝整个跨源请求。

## 后果

依赖跨源跳转的 MCP 配置需要管理员改填最终可信 URL。本约束不等于 IP 白名单、DNS 重绑定防护或禁止合法内网服务；不改变 MCP 禁用生效语义。Yuxi 对外知识库 MCP 服务端入口不在本次改动范围。实验只用本地服务和合成凭据，不访问生产。

## 验证

[真实 HTTP 集成](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/integration/mcp/test_http_origin.py) 在独立 loopback MCP 服务上覆盖 SSE 和 Streamable HTTP：正常连接、相对与绝对同源跳转、发现阶段及已持有工具的后续调用。原实现的 8 个跨主机/端口用例均因目标实际收到带自定义密钥的请求而失败，6 个正向对照通过；修复后全部 14 项通过。接收端记录请求到达事实和合成密钥是否匹配，工具结果确认为 `MCP_ORIGIN_OK`。

[MCP unit](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/unit/services/test_mcp_service.py) 共 26 项通过，覆盖协议降级、主机/端口变化、HTTP 传输别名、配置不被修改及 stdio 配置保持。协议变化使用 MockTransport，未冒充真实 TLS 实验；集成测试使用真实 HTTP 和 SDK，不替换 MCP 协议。完整后端 unit 为 2300 passed、53 skipped、7 subtests passed，跳过项不计入已验证。

```bash
docker compose exec -T api uv run --group test pytest test/unit/services/test_mcp_service.py -q
docker compose exec -T api uv run --group test pytest --confcutdir=test/integration/mcp test/integration/mcp/test_http_origin.py -q
docker compose exec -T api uv run --group test pytest test/unit -m "not slow"
```

本次使用隔离 Compose、当前源码及依赖匹配的已有镜像，以 `uv run --no-sync` 执行。`--confcutdir` 排除面向全应用 API/Sandbox 的自动清理 fixture；本文件自行启动并关闭所有 MCP 服务，不依赖应用账号、PG 或 provisioner。未验证生产地址、TLS 服务部署、DNS 重绑定或外部供应商 OAuth；不将该局部网络边界等同完整网络隔离。
