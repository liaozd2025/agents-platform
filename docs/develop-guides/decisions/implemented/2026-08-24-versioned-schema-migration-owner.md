# 数据库 Schema 迁移 Owner 与轻量版本契约

状态：implemented
类型：architecture
Owner：backend/package/yuxi/storage_migration.py

`storage-migrator` 执行数据库 Schema 变更，`PostgresManager` 持久化版本并提供只读兼容校验；API 与 worker 只消费已经完成迁移的 Schema。

## 问题

API 与 worker 在启动时执行建表和 `ensure_*_schema`，多个运行进程可能并发执行相同 DDL。破坏性 SQL 会在每次启动时重复检查，数据库也没有可回读的 Yuxi Schema 版本事实。现有 `storage-migrator` 已经是 Compose 启动门禁，但需要独占 Schema 迁移。

## 决策

现有 `storage-migrator` 是 shipping 拓扑唯一的 Yuxi Schema 修改者，不新增迁移服务或框架。迁移器持有 PostgreSQL session advisory lock，创建 `yuxi_schema_migrations` 版本表，并分别记录 `business` 与 `knowledge` 域。当前版本为 `business=4`、`knowledge=1`：未版本化的受支持 legacy baseline 执行当前幂等建表与收敛 SQL，已发布 business v1、v2、v3 执行幂等 Schema DDL 后升级到 v4；只有未版本化数据库执行 LangGraph checkpoint setup。当前版本重复运行跳过该域的 Schema DDL，其他非零版本明确失败。

LITE 只迁移并要求 business 域，不创建或要求 knowledge schema；完整模式迁移并要求两个域。API 与 worker 不执行建表、Schema 收敛或 checkpoint setup，只校验所需域等于当前程序版本；版本表或域缺失、过旧或过新时拒绝启动。Compose 继续使用 `service_completed_successfully` 阻止迁移失败后的运行进程启动。

版本表只表达已完成的 Yuxi Schema revision，不承诺自动回滚。破坏性变更必须继续声明数据影响、保持升级幂等并通过真实 PostgreSQL 验证；后续 Schema 变更提升对应域版本并实现受支持的相邻升级路径。

## 替代方案

- 引入 Alembic 和独立 `db-migrator`：当前没有复杂迁移分支需求，会新增依赖、配置和第二个部署服务；拒绝。
- 只把 DDL 移到迁移器但不记录版本：不能避免每次启动重复执行破坏性收敛，也不能让运行进程校验兼容性；拒绝。
- 保留 API 或 worker 的兜底建表：会重新形成多个 Schema Owner，并让错误部署静默修改数据库；拒绝。
- 要求所有迁移支持 downgrade：数据删除和约束收紧无法形成可信无损回滚；采用发布前备份、幂等升级和明确数据影响。

## 后果

- API 与 worker 启动不再竞争 DDL 锁；迁移错误集中在 `storage-migrator`，失败会阻止运行服务启动。
- 当前版本正常重启不再重复执行 Yuxi Schema 收敛 SQL。
- 裸进程启动前必须先运行迁移器；缺失或不兼容版本形成明确启动错误。
- 首次接入时，没有版本记录的已知 legacy/current 数据库执行现有幂等收敛后建立 baseline；现有 Workdir 中间 Schema 检测继续 fail-closed。
- 版本记录与历史文件迁移不是单一数据库事务。中断时版本不推进，Schema SQL和文件迁移依靠既有幂等边界重跑。

## 验证

- `pytest test/unit -m 'not slow'`（隔离容器）覆盖未版本化 baseline、v1/v2/v3→v4、当前 v4、LITE 和失败不推进版本。
- `pytest test/integration/services/test_schema_migration_version.py` 使用真实 PostgreSQL 临时 schema 覆盖 advisory lock、版本 fail-closed、v1/v2/v3 DDL 失败回滚、Project 生命周期与组织快照补列、显式版本推进及 cleanup 行锁。
- v4 尚未执行完整 shipping 升级演练；发布前仍需备份并验证 v1/v2/v3→v4、重复运行和 readiness。
- `python3 scripts/verify_engineering_contracts.py` 与 `python3 -m unittest scripts.test_verify_engineering_contracts` 作为提交前 gate 再次执行。
