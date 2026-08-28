# 官方主线兼容合并边界

状态：implemented
类型：process
Owner：ARCHITECTURE.md

## 问题

产品主线与官方 `upstream/main` 同时修改了 Agent 运行、Project/Workspace、Skills、知识库、Dashboard、认证权限和前端交互。机械选择任一分支会丢失另一侧已经存在的用户能力，尤其可能破坏集团组织、数据权限、Run 因果绑定、文件边界或官方新运行时。

## 决策

以产品主线的九典品牌、集团组织树、多角色数据范围、资源归属、OA 接入和历史 Dashboard 为业务语义；以官方主线的新代码分层、Project/Workspace/Workdir、Memory、统一 Skill runtime、版本化存储迁移和前端交互为新增能力。实际事实分别由 authorization、service/repository、模型/迁移、Compose 和 Web API/store/component 拥有，本记录不成为第二套运行时事实源。

冲突按共同祖先核对双方增量，并沿入口、service、repository、持久化和用户结果检查真实装配。旧 sandbox path、Skills backend、checkpointer config、attachment middleware 和 thread files service 仅在各自 consumer 已迁移到官方 canonical Owner 后移除；没有用删除业务能力的方式消除冲突。

无法同时成立的语义采用以下明确口径：

- 官方单角色/扁平部门与产品多角色/组织树不并存，保留产品 RBAC、组织树和 fail-closed 数据范围。
- 官方当前对象统计与产品历史审计不混用；历史会话、反馈和 ToolCall 使用写入时组织快照，ToolCall 的 self owner 经 Message→Conversation 解析，当前资源库存使用统一 ACL resolver。
- 旧 thread 文件和 `/home/gem` 路径口径不保留，统一采用 Project、Viewer、Workdir 和 sandbox 虚拟路径边界。
- 旧 Skill cache/backend 不保留，统一采用官方 Skill runtime；产品资源权限继续约束共享 Skill，个人 Skill 文件读取从可信根 no-follow。
- PI Local tracer 使用 attempt 独立 Workdir 和临时 Skill 投影；绑定 Workdir 的 Sandbox 命令显式使用严格 `exec_dir`，不依赖远程 shell 继承容器 `working_dir`；Python Manifest 与 Runner 的 Skill 摘要统一使用确定性 Unicode 路径顺序，不依赖运行环境 locale；创建失败、取消和已知启动失败在实例确认释放后清理，final ACK 与 `execution_unknown` 保留 outputs，避免删除已被持久事件引用的文件；释放失败由 attempt 持久化并由 worker 周期重试。
- 用户分页采用官方 `/users/page` 响应 envelope，查询仍由产品管理域 SQL 限制；越出管理域保持 404。
- 品牌、OA 精确部门匹配和产品全屏设置入口保持产品行为，不回退到官方默认文案、自动建部门或旧设置弹窗。
- 新增的 RBAC/组织快照和官方业务表结构共同归 business schema v2，已记录为 v1→v2 相邻升级，避免已版本化 v1 数据库跳过 DDL。
- 未记录 schema version 的产品数据库可能缺少官方 `creation_request_id`；v0.7.1 Workdir cutover 在 ORM 重写前幂等补列，唯一索引仍由后续 business schema v2 统一创建。

## 替代方案

- 全部采用官方版本：代码最接近上游，但会覆盖产品线已有的集团组织、权限和 OA 集成，拒绝。
- 全部采用产品版本：保留现有业务，但无法获得官方的新运行时、Project/Workspace、迁移和前端能力，拒绝。
- 长期并存两套实现并按配置切换：冲突少，但会形成双重事实源和持续维护面，拒绝。

## 后果

- 产品能力与官方新增能力共用一套入口和持久化事实，没有引入长期双实现开关。
- business v1 部署升级前必须备份并由唯一 `storage-migrator` 完成 v2；API/worker 在版本未到 v2 时拒绝启动。
- PI provisioner 未确认释放时不删除 bind-mounted Workdir 或 Skill 投影；多个 worker 通过 PostgreSQL `FOR UPDATE SKIP LOCKED` 排他收敛同一 attempt，成功后才清除 `cleanup_failed_at`。
- Dashboard 资源 ACL 当前在 service 中逐资源、逐主体复用 resolver；数据量出现可测慢查询后再下推 SQL。
- 单元与静态 gate 不能代替真实 worker、SSE、浏览器和签入后业务验收，未执行范围必须继续显式报告。

## 验证

- `pytest test/unit -m 'not slow'`（隔离容器、只读仓库根、可写临时 Skill 投影）：1726 passed，8 warnings。
- AgentRun repository、Skill、PI 和 worker 聚焦 unit：157 passed；Dashboard 与存储迁移聚焦 unit 各 8 passed。覆盖 ToolCall self owner、推断快照 join、资源 ACL service、共享/个人 Skill symlink 竞态、PI 创建补偿、orphan 重试和 v1→v2 升级。
- `pytest test/integration/services/test_schema_migration_version.py`：4 passed；使用真实 PostgreSQL 唯一临时 schema，验证 advisory lock、版本 fail-closed、真实 DDL 失败不推进 v1、成功补列后显式记录 v2 及 PI cleanup 并发行锁，结束后删除 schema。
- `pytest test/integration/services/test_workdir_user_workspace.py::test_v071_thread_layout_migrates_files_empty_workdir_and_attachment_metadata`：1 passed；真实 PostgreSQL 临时 schema 先删除 `project_id` 与 `creation_request_id`，验证 cutover 可在新版 ORM 查询前兼容补列。
- `pytest test/e2e/test_pi_local_tracer.py -m e2e`：4 passed；真实 provisioner 与 PI 镜像验证混合大小写 Skill 树摘要、golden outputs 在 final ACK 后从 Workdir 回读、取消后实例和 attempt scope 清理，以及模型任务认证 fail-closed。
- Ruff 0.16.4 check 与 format check：通过；`git diff --check`、`git ls-files -u`：通过且无未解决索引项。
- Web 锁定安装、ESLint、完整 unit 和生产 build：通过；VitePress 文档 build：通过。
- 工程契约、Compose 校验和完整最终 Git 状态在本次合并交付前再次执行。
- 未执行：会触发现有另一工作树 API/worker 的 live integration、E2E、真实浏览器签入验收，以及完整 shipping business v1→v2 部署演练。
