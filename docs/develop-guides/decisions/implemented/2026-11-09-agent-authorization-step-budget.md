# 授权检查不单独消耗图步数

状态：implemented
类型：bug-fix
Owner：backend/yuxi/modules/agents/runtime/middlewares/authorization.py

## 问题

授权前后检查作为两个独立图节点，使相同 300 步预算可完成的工具轮次减少。显式配置不能用于判断用户是否沿用旧默认值，不能自动覆盖为 2000。

## 决策

### 实现方案

`ModelTurnAuthorizationMiddleware` 将模型前接管检查、协作消息读取与资源刷新收敛为一个 before_model 节点，调用各自 Owner 的检查或消费方法，保持接管 → 协作 → 授权的顺序。协作消息与游标在该节点完成后共同写入 checkpoint；授权失败时不提交本次读取，后续执行按稳定消息 ID 重新读取，不会丢失邮箱消息。SteerMiddleware 保留模型返回后的检查，CooperationMiddleware 保留工具、状态和身份提示装配。

模型返回后保持授权 → 审批 → 接管三个节点的既有顺序，不合并这些不同提交和等待边界。模型结果、用量和模型前警告独立提交，撤权或供应商异常不抹去已产生的事实。内层 RuntimeAuthorizationMiddleware 保留每次真实模型请求、重试和工具执行前的权限检查。

默认审批且开启协作时每轮由 8 步减少至 6 步，不改变显式预算、持久配置或 Run 终态规则。

## 替代方案

- 将所有 300 改为 2000：无法区分人工限制与旧默认值，拒绝覆盖显式配置。
- 删除权限检查：会允许撤权后的模型或工具执行，拒绝。
- 将前后授权移到模型包装：模型失败会丢失本轮警告，模型返回后撤权会阻止已发生用量提交，因此保留模型独立 checkpoint 边界。
- 把返回后的接管检查提前到审批前：部分批准、部分拒绝的恢复路径可能执行原本应被接管阻止的工具，因此保留审批后的接管节点。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
| --- | --- | --- | --- | --- | --- |
| 300 步完成固定 45 次不同工具调用并结束 | 检查节点占满预算 | graph.py / authorization.py | 装配图回归 | 恢复 HEAD 中 graph.py、authorization.py、steer.py、cooperation.py 的容器覆盖层后，生产 v3 入口触发 GraphRecursionError | Passed |
| 模型返回时撤权阻止审批和工具副作用 | 检查顺序改变 | authorization.py | 装配图撤权回归 | 独立身份 oracle 拒绝，工具结果为空 | Passed |
| Skill 注入前刷新、重试前复查、警告可恢复 | 包装位置改变语义 | authorization.py / graph.py | 授权与 Skills 回归 | 撤权、来源变更与能力恢复案例 | Passed |

## 后果

轮次身份复查必须位于审批之前，内层授权必须留在工具动态装配之后。接管命中时仍先于资源刷新和协作消息消费结束。图内模拟不替代真实 HTTP、worker 和 PostgreSQL E2E；执行记录须区分这两类证据。

## 执行证据

隔离镜像 `yuxi-local-review-api:0.8.0.beta6` 只读挂载当前源码与测试；全量 unit 额外挂载 backend/scripts。测试使用一次性 PostgreSQL 16 与 Redis 7.4.10，不连接业务数据。

- `python -m pytest -p no:cacheprovider test/unit/agents/test_execution_step_budget.py -q --tb=short`：5 passed。有限任务经过 BaseAgent v3 入口，回读 checkpoint 的最终文本、45 个不同工具结果、协作消息与消费游标及无待执行节点；覆盖撤权阻止审批、失败警告和用量保留、模型前接管以及混合审批恢复后的接管。
- 相同预算回归使用原始四个生产文件的只读容器覆盖层：失败原因是 `GraphRecursionError: Recursion limit of 300 reached`，工作区保持修复版本。
- `python -m pytest -p no:cacheprovider test/integration/services/test_runtime_authorization_boundaries.py -q --tb=short -x`：16 passed。包括真实数据库撤权、Skill 来源替换、重试、返回前审批边界、能力恢复和 PostgreSQL checkpoint；测试 fixture 使用当前角色分配模型，模型供应商元数据由固定 ModelInfo 提供。
- `python -m pytest -p no:cacheprovider test/integration/services/test_runtime_role_loading.py test/integration/services/test_steer_checkpoint_boundary.py test/integration/services/test_thread_priority_inputs.py -q --tb=short -x`：26 passed。包括真实角色撤销、PostgreSQL checkpoint 中并行工具批次交接、原线程输入优先队列与 HTTP 协议。旧钩子的测试调用已同步到当前入口，checkpoint 测试自建表，隔离测试工作区。
- `python -m pytest -p no:cacheprovider test/unit -m 'not slow' -q --tb=short`：3921 passed、127 skipped；隔离镜像缺少部分宿主/可选环境，skip 不作为业务通过证据。
- 工程契约检查通过，verifier 自测 56 passed；修改文件 Ruff 与 diff 空白检查通过；`cd docs && pnpm run build` 通过。
- 完整 HTTP/API/worker E2E 和真实模型供应商探针未执行：本轮只启动临时数据库服务，没有启动完整应用拓扑。本记录不宣称部署、线上同一会话重放或端到端业务验收完成。
