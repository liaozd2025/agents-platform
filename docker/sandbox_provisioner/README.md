# 旧版 provisioner 源码

此目录保留企业 fork 的旧沙盒实现及资源、网络、Kubernetes 和运行规格约束，供迁移审查使用。当前 Compose、API 与 worker 使用 Container runner 或 Cube，不装配本目录；源码存在不表示这些能力已经迁移。

已承接与尚未承接的限制见 [Container 沙盒](../../docs/advanced/container-sandbox.md#资源与网络)。依赖独立 bridge 或 Kubernetes 的部署，在对应约束实现并验证前保持旧版本。

`test_legacy_config.py` 保留旧实现的负向案例，供比对未承接能力；不进入新运行链的后端 unit 门禁，也不作为 runner 验收证据。当前资源、身份、挂载和生命周期用例位于 `backend/test/unit/sandbox/`。
