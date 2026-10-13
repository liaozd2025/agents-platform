# 单沙盒执行资源上限

状态：implemented
类型：feature
Owner：docker/sandbox_provisioner/app.py

## 问题

上游新增 Docker 内存、CPU 和进程数上限；fork 已在 Docker、Kubernetes、tmpfs、实例容量和复用检查中使用同一份资源规格。合并两套配置会使创建时的 2 GiB 覆盖 fork 的 4 GiB，而健康和复用检查仍读取原规格。

## 决策

### 实现方案

由 `SandboxResourceLimits.from_env()` 统一解析并校验资源配置，Docker 创建和复用校验、Kubernetes requests/limits、诊断使用同一规格。保留 `SANDBOX_MEMORY_MB=4096`、`SANDBOX_CPUS=2`、`SANDBOX_DOCKER_PIDS_LIMIT=512`、`SANDBOX_TMPFS_MB=1024` 与实例容量限制。非法、非有限、过小或相互矛盾的值在创建执行实例前拒绝。

本次[上游合并](2026-10-13-upstream-sync.md)不引入重复的 `SANDBOX_MEM_LIMIT` 和 `SANDBOX_PIDS_LIMIT`。实际配置见[沙盒运维](../../../advanced/sandbox-operations.md)。

## 替代方案

同时保留两套上限会引入优先级及跨后端不一致；只使用上游三项限制会丢失 fork 的 tmpfs、实例容量和复用约束。二者均不采用。

## 后果

保留 fork 已部署的配置名与默认预算。Docker 进程上限仍在容器创建边界执行；Kubernetes 继续使用既有内存与 CPU 资源限制。预算调整需结合实际硬件与任务校准。

## 验证

容器内后端 unit 全集通过，包含 `test_sandbox_provisioner_config.py` 的非法配置、实际创建参数、容量和复用失败用例。真实 Docker 链路验证记录在本次合并决策中。未运行真实 Kubernetes 集群验收。
