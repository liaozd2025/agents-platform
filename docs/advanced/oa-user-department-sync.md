# 旧 OA 用户与部门同步

本页供生产运维人员或受控的 AI 工具执行旧 OA 组织和用户同步。同步脚本从旧 OA 读取在职人员和部门树，再写入当前平台的 `departments`、`users` 与用户角色关联数据；它不处理 H5 历史会话。

## 适用范围

首次上线时按本页完成一次全量同步。后续旧 OA 新增员工、部门或调整员工所属部门时，按相同顺序再次执行增量同步。脚本默认预演，只有明确传入 `--apply` 才写入生产数据库。

常规版本发布不需要重复同步。仅当发布内容首次包含这两个迁移脚本或 `departments` 的 OA 标识字段时，部署完成后执行一次首次全量同步；之后只有旧 OA 数据发生变化或需要重新核对数据时才执行。

同步是幂等的：已存在的部门只补齐或核对 OA 标识，已存在的用户只在部门变化时更新 `department_id`，不会重复创建用户，也不会覆盖当前平台的密码、角色或其他用户资料。

同步补充规则：已存在的用户会按需更新 `department_id`、稳定 UID 和展示姓名 `display_name`，不会覆盖当前平台的密码或角色。展示姓名优先取旧 OA `nickName`（`sys_user.nick_name`）；`username` 始终保留为登录账号。

## 前置条件

- 当前部署版本已包含 `backend/scripts/migrate_oa_departments.py` 与 `backend/scripts/migrate_oa_users.py`。
- API 服务已经启动，且已连接目标生产 PostgreSQL。首次执行时，API 的 schema 初始化会幂等创建 `departments.oa_department_code` 和 `departments.oa_department_id` 及其唯一索引。
- 旧 OA 的 `license.lic` 仅以只读方式挂载到 API 容器，例如 `/run/secrets/oa-license.lic`。文件内容、`SecretKey` 和运行时 `Code` 不得写入 `.env`、命令行、日志、工单或文档。
- 执行账号具备运行 API 容器命令和读取目标数据库统计的权限。执行前完成生产数据库备份，并确认没有其他用户/组织批量写入任务。
- 从受控终端执行命令。迁移会写入员工账号相关日志，生产日志需要按组织的敏感数据策略限制访问和保存期限。

下文使用 `<compose-command>` 表示生产环境实际使用的 Compose 命令，例如 `docker compose -f docker-compose.yml -f docker-compose.prod.yml`。不要把本地端口覆盖文件带到生产环境。

## 推荐：一键同步

部署准备完成后，优先使用统一脚本。它固定按“部门 -> 用户”的顺序执行，避免用户因部门映射尚未建立而被错误跳过。

先执行预演，不会写入数据库：

```bash
<compose-command> exec -T api uv run --no-sync python scripts/sync_oa_data.py
```

确认输出中部门和用户计划都没有 `conflict` 后，才执行写入：

```bash
<compose-command> exec -T api uv run --no-sync python scripts/sync_oa_data.py --apply
```

脚本会从 `OA_USER_MIGRATION_URL` 推导部门接口地址，使用只读授权文件在运行时取得临时 Code；不会把 Code、授权文件内容、人员明细写入日志。默认命令会模拟部门写入后的映射再计算用户计划，因此首次全量同步也能看到可用的用户预演统计。

`--apply` 是唯一写入开关。它会先提交部门事务，再重新读取数据库中的真实部门映射并提交用户事务。两个阶段分别是独立事务：部门成功、用户失败时，部门结果会保留。此时应根据日志处理用户冲突或接口异常，核对后重新执行预演，不能直接盲目重复 `--apply`。

### 一次性部署准备

- 部署包含三个同步脚本的版本：`sync_oa_data.py`、`migrate_oa_departments.py`、`migrate_oa_users.py`。
- 在服务器 `.env` 配置 OA 用户接口、公司编码和授权文件容器路径；真实授权内容只放在受控文件中，不提交到仓库。
- 将授权文件以只读方式挂载到 API 容器，并确认 OA 网络、PostgreSQL 与 API 容器均正常。
- 首次全量写入前备份生产 PostgreSQL；同步期间不要同时执行用户、角色或部门的批量维护。

### 每次同步前

先运行不带 `--apply` 的预演，检查 `conflict` 和 `skip` 汇总；确认无冲突且业务范围正确后才使用 `--apply`。普通版本升级不需要重复同步，只有旧 OA 的人员或组织数据变化、首次启用该能力、或需要重新核对数据时才执行。

## 手动控制与排障

以下四步脚本保留给排障或需要单独控制部门、用户阶段的场景。正常同步优先使用上面的一键命令。

## 首次全量同步

先同步部门，再同步用户。用户脚本依赖已回填的旧 OA 部门 ID，因此顺序不可交换。

### 1. 部门预演

```bash
<compose-command> exec -T api uv run --no-sync python scripts/migrate_oa_departments.py \
  --url <oa-api-base-url>/DrugDevp/QueryDepartmentTree \
  --license-file /run/secrets/oa-license.lic
```

检查输出中的计划统计：首次执行通常包含 `create` 或 `update_identity`；再次执行且数据未变化时应以 `skip` 为主。出现 `conflict`、接口失败或授权失败时停止，不要加 `--apply`。

### 2. 写入部门

确认预演无冲突后，执行：

```bash
<compose-command> exec -T api uv run --no-sync python scripts/migrate_oa_departments.py \
  --url <oa-api-base-url>/DrugDevp/QueryDepartmentTree \
  --license-file /run/secrets/oa-license.lic \
  --apply
```

脚本用旧 OA 部门树的父子路径定位当前部门，并回填 `treeCode` 与旧 OA 节点 ID。现有部门若已有不同 OA 标识，脚本会报冲突并回滚当前事务，不覆盖原值。

### 3. 用户预演

```bash
<compose-command> exec -T api uv run --no-sync python scripts/migrate_oa_users.py \
  --url <oa-api-base-url>/DrugDevp/QueryUserPage \
  --license-file /run/secrets/oa-license.lic
```

检查 `create`、`update_department` 和 `skip` 的数量及跳过原因。用户接口中的 `departmentId` 可能形如 `200004.0`，脚本会先规范化为整数，再精确匹配部门树中已回填的旧 OA 节点 ID；不会按部门名称猜测归属。

以下 `skip` 原因需要保留并人工处理，不能强行导入：

- `部门 ID 为空或格式无效`：旧 OA 用户记录没有可用部门 ID。
- `部门 ID 不存在`：用户指向的部门未出现在已同步部门树中。
- `部门 ID 重复`：当前平台部门映射出现数据冲突。
- `OA 账号重复`、`账号为空`、`姓名为空`：旧 OA 人员数据不满足导入条件。

### 4. 写入用户

确认预演统计后，执行：

```bash
<compose-command> exec -T api uv run --no-sync python scripts/migrate_oa_users.py \
  --url <oa-api-base-url>/DrugDevp/QueryUserPage \
  --license-file /run/secrets/oa-license.lic \
  --apply
```

脚本在单个数据库事务中写入所有可安全关联的用户。新用户使用随机 `uid`、不可知的随机密码哈希和内置 `user` 角色；已有账号不重置密码或角色。大量首次导入会因逐个生成密码哈希而持续数分钟，事务提交前，前端用户管理页不会显示部分结果。

## 结果核验

写入完成后，从生产数据库回读最终状态。以下 SQL 仅展示汇总，不输出员工账号或姓名：

```sql
SELECT
  COUNT(*) FILTER (WHERE is_deleted = 0) AS active_users,
  COUNT(*) FILTER (WHERE is_deleted = 0 AND department_id IS NOT NULL) AS assigned_active_users
FROM users;

SELECT
  COUNT(*) AS department_count,
  COUNT(oa_department_id) AS oa_id_mapped_departments,
  COUNT(oa_department_code) AS oa_code_mapped_departments
FROM departments;
```

用户管理页刷新后，分页总数应与 `active_users` 一致。还应抽查多个不同层级的部门，确认人员数量和归属与旧 OA 一致。API、前端缓存或分页不会改变数据库事实；以数据库回读结果作为迁移成功依据。

## 后续增量同步

旧 OA 数据变化后仍按“部门预演 → 部门写入 → 用户预演 → 用户写入”执行。部门必须先执行，因为新部门或调整后的部门归属需要先在当前平台建立稳定映射。

| 旧 OA 变化 | 是否需要运行同步 | 预期结果 |
| --- | --- | --- |
| 新增员工 | 是，先部门再用户 | 新账号创建并分配部门 |
| 员工调部门 | 是，先部门再用户 | 已有账号只更新 `department_id` |
| 新增部门 | 是，先部门再用户 | 创建部门并回填 OA 标识，随后导入人员 |
| 仅发布 Yuxi 新版本，旧 OA 数据未变化 | 否 | 不执行迁移脚本 |
| 员工离职或旧 OA 删除用户 | 需要人工处置 | 当前脚本不会删除或禁用已有用户 |
| 部门改名、移动层级或删除 | 先预演并人工审查 | 当前脚本不会自动重命名、移动或删除现有部门 |

重复执行不会自动清理当前平台中已存在、但旧 OA 已不再返回的用户和部门。离职、禁用、部门改名、移动和删除会影响权限语义，必须在预演结果、业务规则和审批范围明确后，使用单独的受控操作处理。

## 异常与恢复

`conflict`、旧 OA 授权失败、接口响应失败或数据库约束错误均视为本次同步失败。保留命令输出中的统计和错误分类，不记录敏感值；确认生产数据库未提交预期外的数据后再处理。部门与用户脚本在 `--apply` 下各自使用单事务，异常会回滚该脚本本次尚未提交的写入。

若用户写入过程耗时较长，先检查迁移进程是否仍在运行，再查询数据库汇总。事务未提交时前端仍显示原有数据；迁移进程结束后以数据库回读确认提交结果。不要在同一生产库并发启动第二个 `--apply` 命令。

生产回滚以执行前的数据库备份为准。不要通过手工删除 `users` 或 `departments` 猜测性回滚，因为用户角色、组织路径和其他业务引用需要在同一恢复边界中处理。

## 自动化执行约束

AI 工具或自动化任务只允许自动执行 dry-run 和只读核验。执行任何 `--apply` 前，必须向具备生产权限的负责人展示预演统计、冲突数、跳过原因和目标环境，并取得明确确认。自动化不得读取或输出 `license.lic` 内容、运行时 `Code`、员工账号、姓名或逐条迁移日志。

## 相关实现

迁移行为与数据约束由 [OA 用户迁移决策记录](../develop-guides/decisions/implemented/2026-08-26-oa-user-migration.md) 和迁移脚本拥有；本页只维护生产操作、核验和故障处理说明。
