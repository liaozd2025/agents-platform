# Agent 确认中断与线程状态时序

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/agents/toolkits/service.py

## 问题

主 Agent 只有在配置中显式选择 `ask_user_question` 时才能调用结构化提问工具。需要用户确认的 Skill 因此可能只输出问题文本并以 `completed` 结束，前端无法恢复等待确认状态。与此同时，前端的已读和周期状态请求可能在新 Run 已写入本地 `loading` 后返回旧状态，导致会话列表显示完成而对话仍在运行。

## 决策

`ask_user_question` 是主 Agent 的基础交互工具，不依赖管理员逐个配置。Chatbot 系统提示要求只有后续执行确实依赖用户补充、选择或确认时调用该工具；子智能体继续由现有过滤规则禁止直接向用户提问。

PostgreSQL 接口结果继续拥有最终线程状态。前端为 SSE 写入的本地线程状态维护单调序号；更早发出的已读或周期同步请求返回后，只能更新请求期间未发生本地状态转换的线程，不能覆盖新 Run 的 `loading`。

## 替代方案

- 只修改 `dept-work-report` Skill：其他需要确认的 Skill 仍会复现，而且已安装副本与来源可能漂移。
- 从普通文本检测疑问句并自动中断：自然语言无法可靠区分阻塞确认与普通回答，会引入新的状态猜测。
- 提高侧边栏轮询频率：不能消除乱序响应，还会增加接口负载。

## 后果

主 Agent 增加一个始终可见的基础交互工具；配置中重复选择不会重复装配，子智能体仍不暴露该工具。前端序号只处理同一页面会话中的乱序，不替代服务端状态事实或断线恢复。该修复不修改数据库 Schema、Run 终态集合、SSE 协议或 Skill 文件。

## 验证

| 验收主张 | 语义 Owner | 证据 | 结果 |
|---|---|---|---|
| 未配置工具的主 Agent 仍装配结构化提问，阻塞输入使用该工具 | `yuxi.agents.toolkits.service` 与 chatbot prompt | 聚焦 backend unit；相关 interrupt、Skill 门控和子智能体过滤 unit；真实 PostgreSQL interrupt integration | Passed，73 条 unit、1 条 integration |
| 旧已读和周期同步响应不覆盖新 Run 的 `loading` | `web/src/stores/chatThreads.js` | 两个延迟响应负向用例；完整 web unit、lint、build | Passed，181 条 |
| 受影响代码与仓库 unit 保持兼容 | backend、web | backend `test/unit -m "not slow"`；web build | Passed，1709 条，40 skipped；build Passed |
| 真实页面的运行状态与 PostgreSQL 一致 | PostgreSQL 与会话导航 DOM | 回读运行中 Run；检查活动会话的 `thread-status-loading` 和“正在运行” | Inspected |
| 文档和工程契约可构建 | docs 与工程 gate | docs build；工程契约及其单元测试 | Passed |

未向真实模型额外发送测试对话，因此模型在新提示下实际选择 `ask_user_question` 的 provider 行为未做独立 real-provider probe；工具装配、提示输入、interrupt 协议和前端恢复分别由确定性测试覆盖。
