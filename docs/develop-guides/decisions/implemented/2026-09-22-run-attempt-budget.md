# Run 准备阶段的持久重试预算

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/services/run_worker.py

## 问题

ARQ job_try 在重新投递后可以归一。执行尚未开始时，超时取消释放 Run，其他可恢复故障也仅检查 job_try，因而绕过 Run 的总重试上限。

## 决策

worker 取得执行权的事务同时读取既有 AgentRunAttempt.attempt_no。准备阶段取消与可恢复故障用该序号和现有 WorkerSettings.max_tries 判断预算，当前包含首次在内最多两次；耗尽时 Run 和当前 Attempt 记录 failed/run_retry_exhausted，错误事件标记 retryable=false。旧 pending 已超额时，取得用于收尾的 owner 后立即失败，不再进入准备。计数事实属于 PostgreSQL Attempt，预算及执行边界属于 worker；ARQ job_try 只用于诊断。

用户取消和当前 owner 约束继续优先，已提交终态不可覆盖。终态重复投递与 runtime 清理不创建业务 Attempt。执行开始后的中断继续沿用[不重放边界](2026-09-21-run-execution-retry-boundary.md)，第一次即以 execution_outcome_unknown 失败。

## 替代方案

只检查 ARQ job_try 无法跨新 job 保持预算。新增计数列或配置会重复数据库已有事实和现有上限。两个不可重试失败原因共用已有的终态、取消竞态和事件收尾逻辑，避免在异常分支各自实现一套。

## 后果

准备阶段可恢复故障和 ARQ 超时无法通过反复投递获得新预算。正常请求和预算内准备重试仍可执行。数据库不可用或进程硬杀仍由 lease 恢复收敛；本项限制业务 Attempt 次数，不提供包含所有清理与数据库等待的完整 wall-clock 截止时间，也不调整取消后排队请求策略。生产、外部模型供应商和硬杀矩阵未验证。

## 验证

[隔离 E2E](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/e2e/runs/test_attempt_budget.py) 使用真实 HTTP、PostgreSQL、Redis 和 ARQ worker，模型为确定性本地 HTTP/SSE 服务，MCP 工具把合成副作用写入独立文件。worker 完成真实启动后在测试屏障等待，测试取得 users 表锁再允许领取任务；五秒 ARQ timeout 中断真实用户读取。第一次 Attempt 为 retry_released，新 job 的 result_info 确认 job_try=1，第二次 Attempt 为 failed/run_retry_exhausted；回读 Run、SSE 错误及清理完成，确认没有工具副作用。终态重复投递后仍仅有两个 Attempt。

同一 E2E 的正常请求完成且产物绑定对应 Run；工具先写副作用后阻塞返回的请求，经真实 ARQ timeout 以 execution_outcome_unknown 失败，重复投递不增加动作或 Attempt。显式 HTTP 取消对照为 cancelled。测试另外核验实际 readiness，所有场景均等待 runtime 清理完成。

将 worker 单文件回挂本轮前版本后，同一 E2E 在第二次 Attempt 仍为 retry_released 处准确失败；修复版完整场景通过。

单测覆盖准备阶段取消、连接错误、旧 pending 超额、取消先于预算和并发终态保护；本轮前代码在三个预算回归场景准确失败。全量后端单测2332项通过，53项跳过。

```bash
bash backend/test/e2e/runs/run_attempt_budget.sh <本地依赖匹配的API镜像> <本地provisioner镜像>
docker compose exec -T api uv run --no-sync pytest test/unit/services/test_run_worker.py -q
```

[一次性入口](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/e2e/runs/run_attempt_budget.sh) 使用唯一 project、内部网络、临时目录及 PostgreSQL/MinIO tmpfs，测试成败均销毁本次容器、网络、卷和临时状态，不访问生产或外部付费模型。
