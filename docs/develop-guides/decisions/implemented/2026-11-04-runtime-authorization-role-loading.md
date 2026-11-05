# 执行边界显式加载角色授权关系

状态：implemented
类型：bug-fix
Owner：backend/yuxi/modules/agents/runtime/middlewares/authorization.py

## 问题

运行时权限复查直接查询 User 标量字段，后续技能和知识库权限计算读取尚未加载的角色关系，在异步 Session 中触发隐式查询并抛出 MissingGreenlet。初始运行准备使用完整用户仓库查询，后续模型边界却未沿用相同契约。

## 决策

### 实现方案

执行边界复用 UserRepository.get_by_uid，在显式 await 内加载角色分配、角色权限、组织范围及 APP 所有者关系。返回后仍显式拒绝已删除账号，继续执行原有 Agent 可见性检查。每次模型和工具边界重新读取当前授权，不缓存跨轮次权限。

## 替代方案

不在同步权限计算中触发额外查询，不用异常捕获跳过授权，不把所有 ORM 关系全局改成自动加载。复用现有仓库读取避免建立第二套授权加载配置。

## 后果

运行边界会增加读取授权关系的查询，但保持当前权限撤销即时生效；权限规则、Run 状态及队列提交边界不变。

## 验证

真实 PostgreSQL 隔离 Schema 与真实 LangGraph 模型轮次覆盖有 Run 和无 Run 两种入口：模型返回预期结果；撤销 skill:use 后技能选择被清除；删除账号后下一次模型调用被拒绝。测试使用本地模型，不发送外部模型请求。修复前同一模型前置边界复现 MissingGreenlet，修复后与授权中间件单测共 24 项通过。

相关测试：backend/test/integration/services/test_runtime_role_loading.py、backend/test/unit/middlewares/test_runtime_authorization.py。测试未覆盖完整 ARQ worker、外部模型与浏览器对话链路。
