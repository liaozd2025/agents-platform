# Dashboard 会话审计使用真实 Run 状态

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/repositories/dashboard_repository.py

## 问题

全平台会话审计把 `Conversation.status=active` 显示为“进行中”，但该字段只表达会话未归档、未删除，不表达 AgentRun 是否仍在执行。已完成、失败或取消的 Run 因此长期显示为“进行中”。

## 决策

保留 `status` 作为会话生命周期状态，审计列表和详情另外返回该线程最新 AgentRun 的 `run_status`。前端任务状态列和详情标签只读取 `run_status`；会话状态筛选继续读取 `status`，并把 `active` 标为“正常会话”。

最新 Run 按同一 `conversation_thread_id` 的 `created_at` 与 `id` 倒序确定，覆盖普通、恢复、子智能体和沙箱运行；不存在 Run 时返回 `null` 并显示“未运行”。不修改 Conversation、AgentRun 状态机或数据库 Schema。

## 替代方案

- 在 Run 结束时把 Conversation 改为 `completed`：混淆可继续对话的生命周期与单次执行终态，并破坏现有只查询 `active` 会话的入口。
- 仅把 `active` 文案改为“正常”：消除误导但仍不能满足审计任务状态需求。
- 从最后一条 assistant 文本推断完成：自然语言不是状态事实，失败、取消和中断也无法可靠识别。

## 后果

列表查询增加最新 Run 状态的相关子查询；现有 `agent_runs.conversation_thread_id` 索引支持该读取。若审计页数据规模下出现可测量的查询退化，再改为窗口子查询一次联接，不预先增加复杂查询结构。

## 验证

- Backend service unit：8 passed，覆盖旧 Run 为 `failed`、最新 Run 为 `completed`，以及无 Run 返回 `null`。
- Web unit：10 passed；ESLint 通过。
- Ruff check 与 format check 通过；工程信任检查及其 61 个单元测试通过；`git diff --check` 通过。
- 登录后的真实页面显示 `九典制药股票行情分析` 为“已完成”，列表列名为“任务状态”；真实数据库读取确认该会话仍为 `status=active`，最新 Run 为 `run_status=completed`。
- Dashboard HTTP integration 新增了真实 PostgreSQL 用例，但本地全局 sandbox cleanup fixture 阻塞；现有凭据型 HTTP 用例因未配置 `TEST_USERNAME`、`TEST_PASSWORD` 跳过，未把这部分记录为通过。
