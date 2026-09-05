# PI 沙箱任务的可靠交付与持续协作

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/pi_execution_service.py

## 问题

PI 已在现有 Run 体系中执行沙箱任务，但外层取消不能收敛全部异步任务，执行文件与交付文件混用，动态容器缺少资源限制。一次性 session 和仅工具首尾事件使 Web 用户难以继续任务或判断执行进展。

## 决策

在现有 runtime、Run、文件系统和 SSE Owner 内实现[实施规格](../../../enterprise/2026-09-05-pi-sandbox-web-experience.md)。PostgreSQL final ACK 拥有交付事实；取消先等待执行停止，确认失败保留 orphan，复用 child 的清理须等待根 runtime 回收。

PI 在 Project cwd 工作，通过 `submit_artifact` 登记本 attempt 交付，Workdir 有界 no-follow 回读并校验摘要。同用户、Project 和 child 会话的已 ACK session 由服务端选取和快照，Runner fork 新 session，旧文件不变。逐模型配置与本次 token usage 沿用现有 metadata。

正文和累计工具快照合并进入 Redis SSE，不逐 token 重写 PostgreSQL。根 Request 队列拥有引导唯一消费权；Runner 经固定注释输入和 ACK，在完整工具批次后让位。PI 派生镜像修正基础 `/shell/write` 的 PTY 输入行为，启动 inspector 同时拒绝没有该修补的预构建镜像。

provisioner 强制可配置资源与容量，默认按外部模型 API、64GB 单机设置初始预算。满额创建有界等待；策略不匹配拒绝复用，保留活动实例。具体参数与操作由[沙盒配置](../../../agents/sandbox-architecture.md)拥有，运行语义见[沙盒机制](../../../mechanisms/sandbox.md)。

## 替代方案

- 整个会话改用 PI：改变知识库、MCP、Resume 和 LangGraph 对话合同，不采用。
- 增大机器但不约束任务：无法避免单个失控任务争抢常驻服务资源，不采用。
- 用共享 outputs 扫描补齐交付：会混入并发和历史产物，不采用。
- 为进度建立独立任务/消息存储：与现有 Run 和 SSE 重叠，不采用。

## 后果

session 续接增加持久历史和上下文大小；用量展示区分未知与已上报。审批保持任务级用户工作区访问授权，不新增逐命令审批或同用户 Project 安全隔离。交付 patch 不代表任意源码改动的可回滚 diff。资源预算需在目标机器校准，Kubernetes PID 上限仍由集群 kubelet 负责。

基础沙箱实现被锁定，派生镜像补丁只接受预期源实现；基础镜像升级需要重新验证 PTY 行为。固定模型 E2E 不证明真实供应商质量或生产吞吐。

## 验证

- Passed：集成 backend unit `1871 passed, 45 skipped`；最后的 resume 根清理边界另有 `3 passed` 定向回归，修复前两个负向案例确实失败。工程契约及 `61` 项 unit、Ruff、diff 检查、docs build。构建保留既有 Rolldown/VitePress 警告。
- Passed：真实 Docker 资源/cgroups、配额和旧策略拒绝；源码镜像、预构建及错误摘要的 Compose 启动检查；inspector 包含缺失 PTY 补丁负控。
- Passed：真实 PI SDK 文件边界、session fork、当前 Project 指令、按次 usage、完整工具批次让位；真实 PostgreSQL 归属、lease、未授权 steer final 拒绝，高频事件不写 ledger。
- Passed：`test/e2e/test_pi_http_execution.py` 的四个真实 HTTP 场景分轮通过。前三项验证 9MiB 文件摘要、依赖排除、两轮 session 不改旧字节、每轮 90 token、精确 child state、取消后的文件停止增长与实例移除、非契约上游拒绝。修复 PTY 后单跑 steer `1 passed`（30.94s），验证终态前正文/进度、control ACK、完整工具批次、下轮请求唯一执行。
- Passed：Web lint/unit/build；Chrome 模型字段编辑保存及 HTTP 回读、真实 PI 详情和旧 Run 的未知用量展示。截图在验收会话直接查看，未导出文件。
- Not run：计费供应商、64GB 生产负载、真实 Kubernetes、浏览器运行中 steer 点击及完整断线恢复。开发验证只使用独立 Compose 槽位。早期全量 unit 的两次停顿未定位根因，最终集成全量正常结束；不作为稳定性压测结论。
