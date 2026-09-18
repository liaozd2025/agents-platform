# PI 阶段交接与执行边界

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/services/pi_sandbox_run_service.py

## 问题

模型错误与持久化失败可能被误报为成功或执行未知；主图和普通子图的 PI 审批要求不一致。父子共享运行资源，但原阻断范围只有直接父 Run。业务 Skill 同时依赖主图工具和沙箱执行，阶段交接与数据完整性缺少明确表达。

## 决策

保留 LangGraph 主控、PI 执行及现有 Run、沙箱和工作区。主图负责拆解、审批、用户沟通、知识库与 MCP，PI 负责当前能力与输入范围内的连续执行阶段；文件和 Skill 的读取不要求启动 PI。Skill 可跨执行者，激活 Skill 不会扩展工具或权限。

Runner 仅把正常结束作为成功；模型 error/aborted 不生成 final，即使已收到部分正文。`submit_artifact` 扩展 `stage_status`、`checks` 和 `unresolved_items`，缺输入与失败阶段不发布正式交付文件。没有报告阶段结果的旧任务仍可读取，但主图不能由 Run completed 推断业务完成。

PI execution service 在持久化 envelope 边界把 NUL 转为可见表示，再计算摘要；确定性 JSON/数据库拒绝记录为 `result_persistence_failed`，不按网络错误重复投递。确认 Runner 已退出的模型失败记录为 `model_failed`；无法确认传输或 ACK 时保留 `execution_unknown`。原始文件与 session 不做文本替换。

PI 创建服务依据持久 creator 的审批模式拒绝 default 普通子图直接委派，并要求交回主图审批；worker 执行前重复核实。repository 在既有 Project 行锁内检查与登记，共享 runtime scope 的活跃 PI 串行执行。当前根执行树及合法 resume 中出现 `execution_unknown`、`worker_lease_expired`、`sandbox_parent_unavailable`、`cleanup_failed` 或 `result_persistence_failed` 时，兄弟子图不能绕过停止条件；幂等调用复用原 Run，新的独立 chat 不永久继承旧失败。

mysql-reporter 区分空查询与截断预览，提供真实 SQL 返回行数、展示行数和截断标志；完整 JSON 独占导出不覆盖旧文件。SQL 检查拒绝已知文件副作用与可执行注释，真正权限由业务数据库专用只读账号拥有。主图核对范围、口径、来源及合计，不能对展示子集求全量指标。

本文接续[阶段委派](2026-09-17-pi-delegation-performance.md)并收窄[直接回填](2026-09-18-pi-docx-direct-fill.md)中的通用文档规则：验收方法由任务与 Skill 拥有，不全局禁止版式检查。静默 PTY 观察、进程取消和输出路径边界保持原有语义。

## 替代方案

继续只靠提示词约束无法封闭替代调用入口。每个子任务另建沙箱、增加工作流引擎或数据库阶段状态机都会扩大当前需求；现有 Run、事务、结构化结果及共享域检查已经能表达本次边界。

## 后果

共享运行域保守串行 PI 会限制吞吐，但避免独立上下文同时修改相同文件。执行未知时停止本轮，不实现自动预算退出、进度恢复协议或自动重跑；恢复前仍需核对持久状态与已落盘成果。查询脚本一次获取结果，大明细先限制业务范围或由 SQL 聚合。文本检查与哈希不证明版式、业务口径或数据真实性。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 结果 |
|---|---|---|---|---|---|
| 模型失败不提交成功 final | 先文本后 provider error | PI Runner | 真实 SDK content_filter：`pi_runner_artifacts.test.mjs` 24 passed；adapter unit 覆盖 error/aborted | content_filter；恢复旧错误分支时断言失败 | Passed |
| 文本事件可持久化且错误分类诚实 | NUL JSONB 与确定性 sink 拒绝 | PI execution service / repository | execution unit 与 `test_pi_execution_persistence.py` 真实 PostgreSQL | NUL、SQLSTATE 22P02；去掉 guard 后回归失败 | Passed |
| 子图不绕过审批，共享域不并发执行或绕过未知阻断 | 替代入口、两个兄弟、失联恢复 | PI run service / repository / worker | 三份 PostgreSQL 文件合计 32 passed；worker 57 passed | default 子图、并发登记、只有 PI lease 过期；移除 guard 后能登记第二个 PI | Passed |
| 预览截断不冒充空数据或完整结果 | 超过50行、首行超字符预算 | mysql-reporter | 脚本 unit、三组实际生产函数探针 | 51行最后一行有金额；错误合计被父图拒绝 | Passed |
| 阶段交接与父图验收沿真实执行链成立 | 缺输入、模型失败、截断及NUL输出 | middleware / worker / Skill | `test_pi_http_execution.py` 与 `test_pi_delegation.py`，8 + 3 passed | needs_input、provider error、51行、首行超长 | Passed |
| 静默任务和取消保持进程与文件事实一致 | 120秒观察期限、取消后继续写入 | sandbox backend | `test_pi_local_tracer.py` 6个场景分批通过 | 125秒静默命令、取消/到期后进程消失且文件停止增长 | Passed |

工程契约检查及61项单测、改动Python的Ruff检查、文档构建和diff检查通过。完整unit为2053 passed、45 skipped、1个下述基线失败。

验证在独立 Compose 项目、数据库和当前 Runner 镜像中执行。HTTP 报表场景使用真实 PI 进程、当前生产查询/预览/导出函数和真实文件，仅数据库连接与查询行使用固定 CSV 替代。普通子图审批替代入口由真实 PostgreSQL 创建服务与 worker 测试证明，不声称覆盖完整 HTTP 审批界面交互。

真实外部模型、Charts/知识 MCP、MySQL 连接与账号权限、生产部署均为 Not run。受控响应测试不证明这些外部依赖可用。本次不修改生产配置、账号或已有用户 Run。

复现命令（Compose 指向隔离测试配置）：

```bash
docker compose exec -T -e PYTHON_DOTENV_DISABLED=1 -e LITE_MODE=false api uv run --no-sync --no-dev pytest test/unit -m 'not slow' -q
docker compose exec -T api uv run --no-sync --no-dev pytest test/integration/services/test_pi_scope_boundary.py test/integration/services/test_pi_execution_persistence.py test/integration/services/test_agent_run_manifest_and_attempts.py -q
docker compose exec -T api uv run --no-sync --no-dev pytest test/e2e/test_pi_http_execution.py test/e2e/test_pi_delegation.py -q
docker compose exec -T api uv run --no-sync --no-dev pytest test/e2e/test_pi_local_tracer.py -k 'silent_pty_timeout or cancel_stops_command or delivers_stdout or assembled_path' -q
```

集成与 E2E 必须串行执行：集成测试的 session fixture 会清理该测试环境所有沙箱。全量 unit 中未修改的 `test_enabling_memory_syncs_user_profile_immediately` 失败已在 9f38f125 基线复现，同名模块与 APIRouter 导致 monkeypatch 目标错误；不将全量 unit 标记为通过。
