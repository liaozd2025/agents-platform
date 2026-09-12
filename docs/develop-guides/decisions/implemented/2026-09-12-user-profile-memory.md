# 用户资料记忆与当前工作区边界

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/user_memory_service.py

## 问题

用户资料同步需要适配用户级 Workspace 和多角色模型，在保留手工资料的同时让下一次 Agent 输入反映数据库资料。

## 决策

UserRepository 查询当前用户、部门、Memory 开关与角色分配；仅写入启用角色的名称。上下文构建在读取 USER.md 前执行同步，chat、resume 与状态查看共用该入口。Workspace 提供 no-follow 文件读取与原子替换，资料同步完整读取最多 1 MiB，超过上限拒绝写入，保留原文件。用户关闭 Memory 时移除机器区块，手工内容继续遵循原有上下文加载规则。

## 替代方案

保留旧 sandbox 路径与单角色字段会在当前代码下失效；直接拼宿主路径绕过文件安全边界。采用现有 Workspace 原语和 repository 查询，不增加存储、配置或依赖。

## 后果

同步属于可选准备步骤，失败记录异常并沿用当前可读内容；同步查询使用 savepoint，SQL 查询错误回滚到该保存点。resume 在同步后结束准备事务，再进入流式执行。资料文本不拥有授权能力，后端权限检查保持原样。文件替换是原子的，但并发手工编辑与同步不提供版本冲突检测。

## 验证

单元测试覆盖手工内容保留、幂等同步、Memory 关闭和文件／目录 symlink、超限文件拒绝。真实 PostgreSQL 集成测试通过上下文入口验证角色及部门更新、停用角色过滤、Memory 关闭、最终文件与模型输入一致。工程契约和完整后端回归的执行结果记录于 PR。
