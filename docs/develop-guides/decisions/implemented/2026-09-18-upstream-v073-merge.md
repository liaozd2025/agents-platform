# 合并上游 v0.7.3 并保留 fork 功能

状态：implemented
类型：feature
Owner：backend/package/yuxi/storage/postgres/manager.py

PI 执行相关部分由 [PI 退役决策](../implemented/2026-09-18-retire-pi-executor.md) 取代；本记录中的对应验证仅代表退役前版本。

## 问题

fork 与上游从共同基线分别演进。直接覆盖会丢失 PI 执行、组织权限、知识库 MCP、用户资料及界面定制；仅消除文本冲突不能证明升级可用。

## 决策

以正常 merge 合入上游 v0.7.3（`caff3208c9db9128aa0b277bc5c669141383ddbd`），保留 fork 的 PI、组织多角色权限、OA/OIDC、知识库 MCP、知识来源及嵌入界面。按用户确认移除 LITE；产品版本统一为 0.7.3。

PostgreSQL manager 是迁移 Owner。业务 schema 升至 8，区分原 fork 5 与上游 7；知识 schema 独立升至 3，支持业务提交之后知识迁移失败的重试。权限仍由 fork 的依赖与 repository 执行；定时 Agent 派发重新加载用户的完整角色与部门祖先链。默认四槽 worker 的 durable 后台任务最多占三槽。

运行结果继续按 request/run 绑定。新模型审计与历史投影保留知识来源；准备阶段失败与取消竞态依据持锁后的终态结果收敛。两种审批模式均禁止直接文件写入，实际沙箱任务通过 PI；拒绝调用记录为错误。大结果审批恢复用真实 PI 路径验证原始审计与落盘结果。

前端保留 fork 的权限导航、组织管理和来源弹窗，接入上游任务、历史和工具审计界面；窄屏导航和定时任务工具栏按现有断点换行。

## 替代方案

逐项 cherry-pick 会丢失完整上游版本关系并增加后续升级成本；完整覆盖上游树会删除 fork 当前功能。正常 merge 保留双亲历史，按当前语义 Owner 解决冲突。

## 后果

升级需要先运行 storage migration，API 和 worker 只检查版本。移除 LITE 后不再承诺其轻依赖启动路径。本次仅完成本地合并；未推送、创建 PR、合入远端 main 或部署生产。

验收使用独立 Compose、PostgreSQL 和 Redis，未改动已有服务及数据。真实模型协议由受控 HTTP replay 提供，PI、Docker 沙箱、文件、SSE 和 PostgreSQL 均实际执行；这不证明外部付费模型、生产数据迁移或远端 CI 已验收。测试沙箱复用本地基础镜像并安装当前锁定 runner，未宣称完整生产镜像重建。UI 最小宽度沿用当前 450px；未扩展更窄设备的布局承诺。

## 验证

命令在 `yuxi-v073-check` 隔离 Compose 中执行，前端使用锁文件对应的 pnpm 11.24.0。

| 范围 | 命令或直接证据 | 结果 |
|---|---|---|
| 后端逻辑 | `uv run --frozen --group test pytest test/unit -m "not slow" -q` | 2415 passed |
| 真实 PostgreSQL | `pytest test/integration/services/{test_schema_migration_version,test_agent_request_queue_concurrency,test_agent_run_lease,test_agent_run_manifest_and_attempts,test_scheduled_agent_repository,test_knowledge_source_message,test_durable_task_repository}.py test/integration/storage/test_role_migration.py -q` | 89 passed；包含迁移失败重试、FIFO/lease、定时派发部门链、模型审计来源 JSON 回读 |
| 真实 HTTP | `pytest test/integration/api/{test_agent_run_result_causality,test_agent_request_queue_router,test_context_compression_router,test_platform_permission_router,test_scheduled_agent_api,test_dashboard_router,test_agent_config_resource_authorization,test_oidc_replica_flow}.py -q` | 37 passed；包含刷新历史来源、权限撤销、隐藏资源合并、API Key 幂等输入、OA资料及跨副本 OIDC |
| Worker 与 PI | `pytest test/e2e/test_deterministic_agent_path_e2e.py test/e2e/test_pi_http_execution.py -q` | 13 passed；包含多轮审计归属、两种模式拒绝直接写文件、审批恢复大结果、附件、定时运行、PI交付/取消/steer |
| 旧库升级 | 分别从原 fork `0c0259da` 与上游 tag 的 git archive 创建真实 5/1、7/2 数据库，当前 migration 连续运行两次并回读 | 均收敛到业务8/知识3；中文姓名与OA岗位、角色分配、知识共享范围、文件 indexed 状态及 chunk_count 保留 |
| 前端 | `pnpm run lint:check`、`pnpm run test:unit`、`pnpm run build` | lint/build 通过，434 tests passed；构建保留包体大小提示 |
| 页面 | Chrome 真实登录、设置切换、组织树、任务中心、历史来源弹窗、定时任务表单、浅/深色与450px宽度 | 已截图；来源样例明确标注为本地验收夹具 |
| 工程检查 | `python3 scripts/verify_engineering_contracts.py`、`python3 -m unittest scripts.test_verify_engineering_contracts`、Ruff、`git diff --check`、docs build | 通过；工程检查单测62项 |

独立 Reviewer 审查完整需求、双向 diff、规范和真实测试证据；发现的问题在上述 Owner 处修复，并保留对应回归检查。Git 双亲与上游祖先关系在提交后回读。
