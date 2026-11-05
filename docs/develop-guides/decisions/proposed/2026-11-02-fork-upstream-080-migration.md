# 保留企业定制的上游 0.8 集成

状态：proposed
类型：architecture
Owner：backend/yuxi/modules/identity/permissions/resource_permission.py

## 问题

fork 基线 `921e1ab9` 含企业组织权限、OA 嵌入登录、模型选择、引用、多图和品牌定制。上游目标 `c792eabc` 重组后端领域与前端模块，并以 Input/Turn 替代旧请求队列，替换沙盒和数据库初始化协议。普通三方合并产生 433 个未合并路径，单侧覆盖不能证明定制仍可用。

## 提案

保留双方提交历史，在独立集成分支逐项承接上游变更和 fork 定制。冲突已解决，当前交付为使用新库的隔离开发分支；原 main 和备份分支保留旧基线。用户已授权提交并推送 `integration/upstream-20261102` 至 `liaozd2025/agents-platform` 的同名分支，不合并 main、不创建 PR、不发布生产环境，也不转换现有部署数据。完整旧库迁移尚未实现，因此本记录仍保留 proposed 状态。

### 实现方案

后端采用上游领域目录，企业角色、组织范围和 OA 身份授权仍在身份服务、执行入口和 repository 闭合。新 Input/Turn 执行链消费经权限交集约束的配置，并保留请求与产物归属。前端采用 frontend 模块目录，迁移企业品牌、OA bridge、长时登录定时器和角色管理调用链；独立工具函数先迁移并运行原有行为测试，随后验证真实装配。

数据库版本不通过改数字适配。旧业务版本 10 与上游业务版本 1 的结构不同，组织角色表和会话模型必须单独审计。当前阶段不执行旧数据转换，目标结构为 business 11 / knowledge 4；验证仅使用隔离环境，现有数据保持原状。沙盒从 provisioner 到 runner/Cube 的资源、网络和清理约束须逐项对照，不能把旧配置留在文件中视作功能保留。

基础设施使用新的模块入口、Container/Cube 与 CI 可复用工作流。`docker/sandbox-runner-entrypoint.py` 在启动 runner 前将既有 MB 预算转换为字节，新配置优先；Compose 将旧 worker 并发映射到 `ARQ_MAX_JOBS`。`sandbox_runner/manager.py` 在同一准入锁内按 Docker scope 和 uid 标签执行总量与每用户配额，进程重启不重置名额。资源合法性由 runner 配置边界校验，旧 Kubernetes、独立 bridge 地址池及 browser/full 规格仍未承接。旧实现和负向案例仅作为迁移参考保存，不进入当前拓扑与 unit 门禁。

## 替代方案

完全覆盖上游会移除企业定制，用户已排除。保留旧目录并新增新版入口会形成两条运行链，无法证明旧授权仍被使用，因此不采用。逐项在新语义 Owner 迁移定制保留单一执行路径，但需要独立迁移与回归证据。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 企业定制工具函数迁移后行为保持 | 新目录导入错误 | frontend/src/shared/lib | 对应 node 单测 | 保留原有非法输入和拒绝案例 | Passed |
| 企业角色、组织范围和资源授权在新入口执行 | UI 隐藏代替后端授权 | identity 权限、runtime 与 repositories | 真实 HTTP/PG 的 RBAC、部门、Skill、MCP 与 Dashboard 回归 | 越权角色、跨组织访问及运行中撤权拒绝 | Passed |
| 会话输入与知识工具使用当前运行链 | 旧功能留在闲置目录 | agents Input/Turn、session 与 Knowledge MCP | 真实模型与 ARQ worker 完成知识问答，回读 Run 和工具审计；Session 历史通过真实 HTTP/PG 验证 | 自动 root/child 不进入 Web 历史，Public 完整会话语义保持 | Passed |
| 旧库保持显式拒绝且新库可初始化 | 仅改版本号绕过校验 | migrations/schema | 真实 PostgreSQL Schema 初始化、重跑与版本检查 | business 10 / knowledge 3 不允许直接启动 | Passed |
| runner 保留企业资源预算与每用户容量 | 单位错换、无效预算、满额越过准入 | docker/sandbox-runner-entrypoint.py 与 sandbox_runner/manager.py | 预算子进程测试、runner manager/client 52 项、Compose 配置 20 项 | 非有限 CPU、超预算 tmpfs、同用户满额拒绝 | Passed |
| 构建入口和文档可审阅 | OA origin 遗漏、CI 新入口失效、旧 Owner 路径 | frontend Dockerfile、scripts/ci、部署文档 | CI 脚本 96 项、docs build、开发/生产 Compose config | 非法 origin 拒绝、CI 选择与结果负控 | Passed |
| 交付仅更新获授权的开发分支，现有部署数据保持原状 | 误推 main/upstream 或复用业务数据卷 | Git / Compose | 分支、remote 与隔离配置回读；推送结果由交付报告单独记录 | 禁止向上游写入、合并 main 或复用现有状态目录 | Inspected |

上述 Passed 只对应本行明确列出的观察边界。OA 外部服务、浏览器完整验收以及未列出的引用、多图、Office 和协作 E2E 不由这些结果替代；旧数据转换为 Not run，转换实现尚不存在。Git 的 Inspected 不声明推送已经完成。

## 风险

本地隔离 Compose 已启动 API、worker、PostgreSQL、Redis、对象与检索服务及 Container runner。后端与前端 unit、前端 lint/build、工程契约和文档构建已有通过记录；企业权限、资源授权、Session 历史及 Dashboard 使用真实 PostgreSQL/HTTP 回读验证。最终提交的完整命令、结果和未验证范围由交付报告记录，不以测试数量代替业务事实。

真实模型与 ARQ worker 的知识问答样本完成并回读到工具审计；它不证明全部 worker、引用、多图、Office、协作、故障恢复或压力场景已经验收。浏览器控制通道不可用，尚未完成修复后的逐页真实浏览器验收。旧 business 10 / knowledge 3 的转换、旧 Kubernetes/bridge/browser/full 沙盒能力和旧大规模恢复压力样本仍未完成；不得将本开发分支直接用于旧生产数据。

新增的部门访问、内置 Skill、运行边界角色加载、Session 历史、Dashboard 组织隔离、知识库模型错误、供应商鉴权错误和 MCP HTTP 同源回归均使用独立 Schema 或自建 HTTP 服务，接入现有独立服务 CI。CLI 已采用当前 TypeScript 包的 Node 22、`npm ci`、typecheck、test 和 pack:check；不保留已退役 Python CLI 的 CI 入口。


## 执行恢复与旧测试迁移

`modules/agents/services/runner.py` 在首次消费执行流后拒绝把基础设施中断和连接故障释放为 pending，按 `execution_outcome_unknown` 收敛，避免未知外部副作用被自动重放。执行前的重试预算由同一 claim 事务返回的持久 Attempt 序号决定，ARQ 的新消息不能重置预算。取消与已提交终态仍优先保留。后台 Job 在全局上限内为交互运行保留一个 worker 槽，单槽配置仍可执行后台任务。

恢复扫描在现有 Thread 行锁入口使用 `skip_locked`，实时提交保留等待语义；扫描仍逐线程推进，单个失败不阻断后续线程。checkpoint 初始化在持有 advisory lock 的同一物理连接执行 DDL；初始化错误只记录异常类型，不输出可能包含连接串的驱动异常正文。

以下迁移只针对新运行入口。旧测试文件的完整历史保留在 fork 基线 `921e1ab9`，不通过保留旧导入路径建立第二套运行链。

| 旧测试与增量 | 当前承接位置 | 取舍 |
|---|---|---|
| `test_agent_request_queue_service.py`、`test_agent_request_service.py`：FIFO、提交后投递、恢复串行 | `test_agent_scheduler_recovery.py`、`test_agent_input_concurrency.py`、`test_run_attempt_retry_budget.py` | Input/Turn 替代 Request；保留串行、跳过忙线程和失败隔离 |
| `test_run_worker.py`：持久重试预算、未知副作用不得重放、取消竞态、异常组用量 | `test_agent_run_runner.py`、`test_worker_settings.py`、`test_run_attempt_retry_budget.py` | 在新 runner 与当前 lease Owner 执行，不迁入旧 chunk 协议 |
| `test_tasker_behavior.py`：后台 claim 容量 | `test_background_jobs.py` | 使用当前 worker 配置计算后台上限，验证 1/4/10 并发配置分别为 1/3/4 |
| `test_subagent_run_service.py`、`test_agent_run_service.py`：身份与结果归属 | 当前协作、Run runner、Input 接入与结果测试 | 协作成员共用当前执行器；旧 PI 任务与历史库转换不属于 shipping 入口 |
| `test_chat_stream_interrupt.py`、`test_chat_service_langfuse_stream.py`、`test_chat_service_sync.py` 中的旧函数 | 保留这些文件中的新执行协议测试，保留已显示部分回答、取消 Owner、中断 ID 与终态提交断言 | 只移除已不存在的旧 resume/chunk/PI 接口；知识来源采用当前 MCP/Turn 审计，不恢复旧预取 metadata 注入 |
| `test_conversation_queue_history.py`、`test_conversation_thread_status.py` | `test_session_queue_history.py`、`test_session_thread_status.py`、`test_turn_references.py` | 保留已消费后取消的用户问题；相同工具调用 ID 不同 Run 不串引用；APP 与线程作用域继续拒绝越权 |
| `test_conversation_repository.py` | `test_session_repository.py`、`test_agent_run_repository.py`、组织快照测试 | 会话与 Run 保存创建时快照，Message 沿 Run、评价沿 run_id 读取；已移除的 ToolCall 表不再承担单次写入时重取组织的契约 |
| `test_storage_migration.py` 和旧 `ensure_business_schema` ALTER/回填断言 | `test_schema_bootstrap.py`、`test_postgres_manager_schema.py`、身份迁移约束测试 | 新库保留 OA、组织和角色字段；旧 business 10 / knowledge 3 明确拒绝，旧版本转换和 PI 收敛仍未实现 |
| checkpoint 同连接锁与数据库日志 canary | `test_langgraph_checkpointer_setup.py`、`test_checkpoint_setup_connection.py`、`test_postgres_logging.py` | 恢复同连接 DDL 与异常脱敏，不恢复旧迁移主链 |

验证边界：执行流故障与状态竞态使用当前 runner 的单元回归；持久 Attempt、恢复行锁和 checkpoint 建表在独立 PostgreSQL Schema 回读。真实模型、完整 worker/Redis/Container 拓扑 E2E 不由这些结果替代。APP 管理另经临时 TCP HTTP 服务验证当前 owner 范围与删除后回读。


## 集成与 E2E 协议收敛

`test_deterministic_agent_path_e2e.py` 和旧 SubAgent wire 用例由当前生命周期、Key 作用域与会话协作 E2E 承接；旧 `/api/agent/runs`、agent-call/eval 适配器不再装配，公开 Session 入口仍要求认证。旧 Run 结果隔离由 `test_turn_result_causality.py` 验证 Turn.result_run_id 与 Run.output_message_id 的明确绑定；旧 Request 并发由 Input 并发、优先队列与恢复行锁测试承接。checkpoint 阅读用例保留已下架模型不阻断读取，并拒绝 checkpoint 伪造公开消息与协作成员。

Office 解析增量迁到 `test_office_backend_e2e.py`：通过当前 Session/Turn 事件执行 `ocr_parse_file`，回读原件、Markdown、工具审计与子 Run 数量；已接入 `system-tests.yml`。确定性模型保留 Office 工具名、原文、产物路径和调用身份的拒绝条件，独立 HTTP/SSE 回放验证两阶段协议。该 E2E 仅完成收集，完整 worker 执行仍为 Not run。

旧 `CITATION_REPLAY` 的 `subagent_start/subagent_await` 与 cite 标签协议不再保留；当前引用由 MCP/Turn 读取及跨 Run 引用隔离测试拥有。完整的新 worker/协作引用 E2E 没有因为旧测试删除而视为通过。旧 API Key Schema 升级、v0.7.2 文件属主转换和 PI 历史结构转换不在 shipping 初始化入口；历史数据保持显式拒绝，当前新库结构与用户/Key 生命周期测试保留。

新 app-init 的全局 Schema 锁同时绑定其 DDL、版本发布和共享内容同步 Session。连接失效后，后续操作拒绝自动重连；不能一边失去初始化锁，一边从连接池借新连接提交。`test_schema_migration_version.py` 使用独立 PostgreSQL Schema 验证同一 backend PID、真实断开连接后的事务回滚，以及旧 owner 无法继续发布版本。身份约束触发器保持原有定义，不恢复旧数据转换。

当前收敛验证：105 项协议单测、25 项真实 Schema PostgreSQL 测试、10 项独立数据库 lease 测试通过；110 项相关 integration/E2E 用例收集通过。RapidOCR 在锁定依赖的临时测试容器中补齐 wheel 自带三份模型后，模型资产 6 项测试通过；shipping Dockerfile 已拥有同样的构建期预置步骤，此操作不代表新镜像已部署。


### 旧恢复进程与离线转换测试的边界

`test_pi_retirement.py` 与 `test_role_migration.py` 执行旧 storage_migration、PI 历史 HTTP 和单角色列转换，随旧执行入口退役；它们不能证明 business 10 / knowledge 3 已转换到 11 / 4。当前由 Schema 拒绝旧版本、真实 RBAC 同步与 APP 角色约束测试保护新库；runner 的非法类型回归明确包含旧 `sandbox` 类型，不恢复 PI 执行或历史转换。

旧 `test_recovery_scan.py`、`recovery_scan_process.py` 及 recovery-scan shell/Compose 使用 Request/Conversation 队列；旧 `budget_worker.py`、attempt-budget 测试及 shell/Compose 使用 server.worker_main 和旧 Run HTTP 协议，一并退役，避免留下仍可执行的错误入口或假 skip。当前负例由 `test_agent_scheduler_recovery.py` 的串行/失败隔离、`test_run_attempt_retry_budget.py` 的真实行锁及持久序号、`test_agent_run_runner.py` 的执行后不重放及预算耗尽覆盖，并由现有 CI 调用。旧 193 线程、双独立进程、Redis/ARQ 副作用的压力 E2E 没有在新协议重跑，小样本 PG 与 unit 结果不替代该压力样本。
