# MCP 工具在执行前核对禁用状态

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/agents/mcp/service.py

## 问题

工具和服务器禁用会清理当前进程缓存，但运行中的智能体仍持有旧工具对象，后续调用可以继续到达远端。缓存失效不能收回已交出的执行能力。

## 决策

统一工具加载入口使用现有 MCP adapter 的 tool interceptor。每次调用在建立远端会话或启动 stdio 进程前读取 PostgreSQL，确认服务器仍启用且工具未被禁用。禁用或删除抛出 ToolException，经既有工具错误处理返回模型可见的错误结果；数据库读取失败向上抛出，停止执行。普通工具、Skill、动态加载与管理工具列表共用此入口，缓存中的旧对象保留相同检查。

配置与禁用状态继续由 MCPServer 及现有配置查询拥有。不增加状态缓存、跨进程通知、配置开关或依赖。低层 get_mcp_client 负责协议连接；供平台使用的工具统一通过 get_mcp_tools 装配执行检查。

## 替代方案

只清理缓存无法收回已交出的工具对象，跨进程广播也无法可靠覆盖旧对象。复制或包装每个工具的 coroutine 会重复 adapter 已有的执行拦截机制。

## 后果

每次工具调用增加一次数据库读取，数据库不可用时工具停止执行。检查与远端执行不构成跨系统事务；禁用提交先于执行检查时生效，已经通过检查或已在远端执行的调用不撤回。重新启用后，原工具可以继续执行。本约束不负责连接配置热替换、删除后同名配置重建的对象身份、工具展示、权限细分或请求取消。

## 验证

[执行禁用集成测试](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/integration/mcp/test_mcp_execution_revocation.py) 使用真实 PostgreSQL 独立 schema 和真实 MCP 服务。调用进程持有工具，另一 Python 进程调用产品管理 service 并提交禁用或删除，不通知调用进程清理缓存。SSE 与 Streamable HTTP 的 6 个负向用例在修复前全部因远端收到请求而失败，修复后通过，并验证模型可见 error、未禁用工具正常以及重新启用后的成功结果。

数据库表不可访问用例证明查询失败时远端收不到请求。内置 stdio 用合成服务验证正常结果与启动文件，禁用后文件不再增加，证明未再启动进程。该文件 8 项通过，与重定向凭据边界回归合计 22 项通过；MCP unit 26 项通过。

```bash
docker compose exec -T api uv run --group test pytest --confcutdir=test/integration/mcp test/integration/mcp -q
docker compose exec -T api uv run --group test pytest test/unit/services/test_mcp_service.py -q
```

实验使用隔离 Compose、内部网络和一次性 PostgreSQL。运行前必须在 api 测试容器设置 TEST_MCP_POSTGRES_URL，使用 postgresql+asyncpg:// 格式指向允许创建独立 schema 的测试库；未配置时明确失败。数据库连接工厂仅由 fixture 指向隔离 schema，实际查询和管理写入均走产品代码。内置 stdio fixture 只替换代码拥有的服务定义，不扩大产品可配置传输范围。测试使用依赖匹配的已有镜像及当前源码，以 uv run --no-sync 执行；--confcutdir 排除不相关的全应用 API/Sandbox 清理。没有访问生产、外部供应商或付费模型；不声称完成全应用 HTTP 管理权限、LangGraph/ARQ 运行或生产容量验收。
