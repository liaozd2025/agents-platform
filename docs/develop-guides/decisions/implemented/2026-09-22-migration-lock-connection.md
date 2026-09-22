# 迁移锁与数据库写入共用连接

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/storage/postgres/manager.py

## 问题

Schema migrator 使用独立连接持有 session advisory lock，业务事务另从连接池借用连接。持锁连接被终止后，另一迁移者可取得锁，旧迁移者仍能提交数据。checkpoint setup 同样存在持锁连接和实际执行连接不同的问题。

## 决策

Schema 迁移上下文中的 DDL、版本记录和业务 Session 使用持有 advisory lock 的同一物理连接。各阶段继续独立提交，保留 business 已提交、knowledge 失败后的恢复语义。连接失效时，数据库中断其未提交事务；重新取得 Session 或 Schema 事务的入口拒绝失效连接，不从连接池替换连接后继续迁移。

连接绑定属于当前异步上下文，普通 API/worker 继续使用原连接池。迁移主链路按既有顺序执行，遇到数据库异常即退出；不支持捕获断连后 rollback 并在同一 Session 中重试。重试需退出迁移上下文，重新取得锁。ContextVar 会传播到子任务，因此本决定不提供同一迁移上下文的任意并发事务能力。

checkpoint 迁移使用自己的 psycopg 连接同时持有 checkpoint advisory lock 和执行官方 setup。返回给运行时的 saver 仍绑定原连接池，不保留已经归还的迁移连接。无法确认锁已释放时，持锁物理连接被废弃，不放回连接池。

## 替代方案

定期探测锁并取消 Python Task 存在两次探测之间的写入窗口。把所有迁移合成一个事务会改变已发布的阶段提交与恢复契约。新增 fencing 表会重复本项可以直接使用的数据库连接边界。

## 后果

真实丢锁后，旧数据库事务不能独立提交，后续版本写入不能通过新连接继续。正常迁移、中断重跑与任务间锁竞争保持原语义。调用方若将来引入 Session 内异常重试或并行迁移，必须重新设计并验证连接与所有权边界，不能从当前线性流程的结果外推。

PostgreSQL 锁不是文件系统事务；正在进行的线程文件复制和删除仍依赖原停机、幂等和重跑机制。文件副作用的跨进程重叠留作独立补查，本项不宣称所有外部副作用均有 fencing。生产迁移未执行。

## 验证

[真实 PostgreSQL 回归](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/integration/services/test_schema_migration_version.py) 为每组建立独立 Schema 与唯一 application_name，终止连接时同时限定当前数据库和测试标识，避免误杀其他 migrator。分别覆盖旧业务事务提交和旧版本写入：原实现分别出现两个 marker 和旧版本99覆盖新版本2；修复后只保留新 owner 的 marker 与版本2。

checkpoint 用确定性迁移 SQL 阻塞真实官方 saver，终止它的持锁连接后独立查询目标表。原实现仍创建目标表；修复后目标表不存在，新持锁连接重跑后恰好一行且迁移版本为1。只替换测试迁移输入，未 mock 连接池、锁、SQL 或提交。

完整 Schema integration 共18项通过，包括正常迁移互斥、真实旧版本升级、用户资料与知识文件 owner 保留、业务已提交而知识阶段失败后的重跑。相关 integration 与 unit 共58项通过；后端全量2332项通过，53项跳过。负向对照共三项均在目标缺陷处失败。

```bash
docker compose exec -T api uv run --no-sync pytest test/integration/services/test_schema_migration_version.py -q
docker compose exec -T api uv run --no-sync pytest test/unit/storage/test_langgraph_checkpointer_setup.py test/unit/storage/test_postgres_manager_schema.py test/unit/services/test_storage_migration.py -q
```

验收运行于本地独立 Compose project、内部网络与 PostgreSQL tmpfs，使用合成数据；不访问生产，不把本项测试当作全部文件迁移和跨存储原子性的证明。
