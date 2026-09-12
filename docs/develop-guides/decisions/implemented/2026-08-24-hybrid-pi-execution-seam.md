# 混合 PI 执行 seam

状态：implemented
类型：architecture
Owner：docs/adr/0004-hybrid-pi-execution-seam.md

## 问题

按 [ADR 0004](../../../adr/0004-hybrid-pi-execution-seam.md) 落地首期 Local tracer，并把实现前矩阵收敛为可重复执行的 oracle。本记录只拥有机制验收，不重复架构方案、替代或后果。

## 决策

`POST /api/agent/runs` 通过显式 `executor=pi` 进入现有队列与 worker。worker 在 claim 时把 Local adapter、选路快照和 Runtime Manifest 冻结到 `AgentRunAttempt`，随后由统一 PI execution seam 调用 attempt 独立的 Local sandbox。Result Sink 复用 `AgentRunRepository`，在 ACK 前从宿主 outputs 回读并校验 artifact、patch 与 session ref，再以稳定 envelope 幂等写入 attempt，并把 final Message、输出指针和 Run 终态放在同一事务；ACK 响应丢失时只重放同一 envelope，不重跑 PI。final ACK 后显式删除实例，只保留服务器可读 outputs。

## 替代方案

架构替代及拒绝原因由 ADR 0004 唯一拥有；本记录不重新裁决。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 运行请求、运行与运行尝试仍是唯一状态事实 | PI 另建 Task/attempt 或绕过终态 CAS | `AgentRunRequest`、`AgentRun`、`AgentRunAttempt`、`AgentRunRepository` | `backend/test/integration/services/test_agent_run_manifest_and_attempts.py` 与既有 causality 测试 | 重复 claim、旧 attempt final、相邻 Run 输出不能覆盖 | `Passed` |
| attempt 在 PI 启动前冻结 adapter 与 Runtime Manifest | 运行中换端或实际运行时不匹配 | `AgentRunAttempt`、`AgentRunRepository.mark_running`、PI 执行模块 | `backend/test/unit/services/test_pi_execution_service.py`、`backend/test/integration/services/test_agent_run_manifest_and_attempts.py` | 篡改 PI/Node/Skill bundle 或逐项 digest 必须在 execute 前失败 | `Passed` |
| 重放 event/final 只产生一个逻辑结果，ACK 后结果仍可读 | Redis 重放、ACK 响应丢失、伪造 ref 或旧 attempt 覆盖 | PI Result Sink、`AgentRunRepository`、服务器 outputs | 同上，以及 `backend/test/e2e/test_pi_local_tracer.py` | 首次 final 已提交但响应丢失、错误 path/digest、同一 `event_id`/final 重放两次、旧 attempt 提交 final | `Passed` |
| 取消形成单一终态并停止绑定实例 | 只停 worker stream，sandbox/临时目录继续存在 | 既有 durable cancel、`RunContext`、PI execution seam | `backend/test/unit/services/test_pi_execution_service.py`、`backend/test/unit/services/test_run_worker.py` 与真实 E2E | 执行中取消、event 间取消、delete 瞬时失败、stop 重放 | `Passed` |
| Local golden Task 加载锁定 Skill 并在 rootfs 删除后保留结果 | Skill 漂移、结果只留在 sandbox | Runtime Manifest、只读 Skill bundle、Result Sink | `backend/test/e2e/test_pi_local_tracer.py -m e2e` | 缺失/篡改 Skill，ACK 后删除 sandbox 再读取结果 | `Passed` |
| 既有 LangGraph、FIFO 与 Run seam 不受 PI 显式选路影响 | 默认请求误入 PI 或 PI 绕过队列 | `RunSubmissionCommand`、`agent_request_queue_service`、`run_worker` | 相关 unit 回归集合 | 默认 executor、PI executor、manifest 固化失败 | `Passed` |

本票实现时的聚焦验证：PI、worker、submission、FIFO 与 repository 的 114 个 unit 通过；真实 PostgreSQL manifest/attempt/Result Sink 的 9 个 integration 通过；固定镜像中的真实 PI SDK 成功/取消 E2E 2 个通过。镜像从固定 base digest 构建，Node `22.21.0`、PI `0.84.2` 及 npm integrity、Runner digest、只读 `pi-golden` bundle/逐项 digest 均在启动前比对；artifact、patch、日志、session 与 final 在 rootfs 删除后仍可读。

相关 unit 从仓库根目录使用以下实际命令运行：

```bash
docker run --rm --network none \
  -v "$PWD/backend/package:/app/package:ro" \
  -v "$PWD/backend/test:/app/test:ro" \
  -v "$PWD/backend/server:/app/server:ro" \
  -w /app yuxi-api:0.7.2.dev0 \
  /usr/local/bin/python -m pytest \
  test/unit/services/test_pi_execution_service.py \
  test/unit/services/test_run_worker.py \
  test/unit/services/test_run_submission_service.py \
  test/unit/services/test_agent_request_queue_service.py \
  test/unit/repositories/test_agent_run_repository.py -q
```

## 后果

- T2 的 event ledger 随 attempt 以小型 JSON 保存；出现高频流式日志或独立事件查询需求时再拆表，不能让 Redis 投影成为 ACK Owner。
- PI 已启动后的不确定故障以 `failed + execution_unknown` 收敛，不能释放 lease 创建新 attempt 自动重跑。
- 实例删除瞬时失败原地重试；仍无法确认时在主终态之后写入原 attempt 的独立 `cleanup_error/cleanup_failed_at`，保留 `instance_id` 供 orphan 收敛，不能覆盖取消或 execution_unknown，也不能自动重跑 PI。
- Local artifact/patch/session 由宿主 outputs 拥有；Tencent 仍需服务器上传 Owner，不能复用远端 rootfs、NodePort 或临时 URL。
- 第一阶段只接受随代码审核、逐项锁 digest 的 `pi-golden` bundle；项目与个人 Skill、交互式 RPC、容量阈值和腾讯凭据继续后置。
- 部署 Local PI 前必须构建 `docker/pi_sandbox/Dockerfile` 并把 provisioner 的 `SANDBOX_IMAGE` 指向该固定镜像；运行时不匹配会在 PI 启动前 fail closed。
