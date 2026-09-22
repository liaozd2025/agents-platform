# AgentRun 执行开始后不自动重放未知结果

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/services/run_worker.py

## 问题

worker 正常停止会把正在执行的 Run 释放回 pending。外部工具已完成动作但尚未返回时，新 Attempt 会重新输入原请求，导致副作用重复。

## 决策

worker 在首次消费 chat、resume 或 subagent 执行流前标记本次执行已经开始。仅执行前的基础设施取消和可重试错误仍允许释放为 pending；执行开始后传播到 worker 的同类异常通过既有 lease fence 把 Run 和当前 Attempt 写为 failed/execution_outcome_unknown，发布不可重试错误与终态事件，禁止自动重放原输入。错误提示用户部分操作可能已经生效，先核对结果再决定是否重新发起。

用户明确取消仍使用原取消流程；写失败时发现并发 cancel_requested，继续按用户取消收敛。已提交终态不被覆盖。lease 丢失由既有过期恢复收敛。执行流内部已转成 error 或 interrupted 事件的故障保留原有事件语义；本项不统一流内异常分类。终态 runtime cleanup 仍可以重试，但不会重新进入执行流。状态转换和 Attempt 终止由 AgentRunRepository 拥有，本地标记只裁决当前异常是否允许交回执行权；不新增持久列或旁路状态。

## 替代方案

仅检查 ToolCall 审计不能证明未产生副作用，因为审计事件与远端动作不是同一事务。所有工具采用持久幂等协议需要各副作用 Owner 的支持。当前采用更保守的执行流边界，不增加工具分类、幂等缓存或恢复状态机。

## 后果

进入执行流后，即使尚未产生真实副作用也不自动重试。失败不代表外部操作已经回滚，也不承诺 exactly-once。已经提交的 Run 终态与清理恢复保持原语义。用户核实后可以明确提交新请求，本项不增加跨请求幂等或统一重试预算，也不撤回历史消息、已保存文件或外部写入。

## 验证

[worker unit](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/unit/services/test_run_worker.py) 通过 process_agent_run 入口覆盖三种运行类型的基础设施取消、连接错误及不再释放重试；保留执行前取消、释放失败和临时故障后成功的对照，并验证并发用户取消与已提交终态不被覆盖。旧代码六个新增负向用例均因尝试释放重试失败，修复后通过。

[真实 E2E](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/e2e/runs/test_execution_retry.py) 使用真实 HTTP、PostgreSQL、ARQ worker 和 MCP：确定性工具先追加合成计数文件，后等待返回；宿主脚本收到文件屏障后对专用 worker 执行正常停止并重启。测试随后显式重复投递同一 Run，等待 job 返回并回读数据库与服务端文件。正常对照 completed 且计数一次；中断场景计数一次、Run 与唯一 Attempt 均 failed/execution_outcome_unknown、runtime_cleanup_pending=false，事件包含明确错误。

将同一环境的 worker 单独挂回旧 run_worker.py，最终计数为二且 Run completed，测试准确失败；修复后相同测试通过。副作用计数来自 MCP 服务端独立文件。

```bash
docker compose exec -T api uv run --group test pytest test/unit/services/test_run_worker.py -q
bash backend/test/e2e/runs/run_execution_retry.sh <本地依赖匹配的API镜像> <本地provisioner镜像>
```

[一次性运行入口](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/e2e/runs/run_execution_retry.sh) 复用知识库撤权 E2E 的隔离 Compose 拓扑，仅覆盖确定性模型/MCP fixture；唯一 project、内部网络、临时目录和 PG/MinIO tmpfs 隔离测试数据。EXIT trap 无论成功或失败均销毁本次容器、网络、卷及临时目录。只使用合成账号与本地计数，不调用付费模型或生产服务。

真实故障注入覆盖 SIGTERM 正常停止；强杀、数据库不可用及执行超时的实际故障矩阵不属于本项 E2E 验收。它们仍受现有 lease fence、unit 及本次执行边界约束，不据此声称已验收所有外部工具或生产环境。
