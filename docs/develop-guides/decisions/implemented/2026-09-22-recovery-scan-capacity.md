# 恢复扫描串行跳过被锁线程

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/services/agent_request_queue_service.py

## 问题

恢复扫描为所有 pending 或 queued scope 同时创建派发协程。192 个 Conversation 被锁定时，两个恢复进程各自占满 160 个业务连接，合计 320 个连接等待同一批锁。扫描压力随积压及 worker 副本放大。

## 决策

恢复扫描逐个处理去重 scope，并在恢复专用路径使用 PostgreSQL `FOR UPDATE SKIP LOCKED`。每进程同时最多一个派发事务；被占用线程留给后续扫描，其他线程继续前进。实时提交与显式派发保留原有阻塞行锁。Run、Message 和 Request 仍同事务写入，提交后再发布 ARQ。

## 替代方案

仅用 semaphore 限并发仍会让少数被锁线程拖住整轮；仅 SKIP LOCKED 仍可能瞬间为全部 scope 申请连接。新增中央恢复锁或租约增加持久状态与恢复职责，没有当前必要。顺序非阻塞扫描复用数据库原生能力。

## 后果

扫描为顺序执行，不承诺单轮大型积压的最短处理时间；被锁线程最长等待后续周期。范围不包含池配置统一、生产容量或恢复期间执行模型。使用合成数据和隔离服务，保持普通请求的锁语义。

## 验证

`backend/test/e2e/runs/run_recovery_scan.sh` 使用一次性 PostgreSQL/Redis、192 个被锁线程和一个可派发线程，以及两个独立 Python 恢复进程。池 checkout/checkin 记录业务连接峰值，独立 SQL 连接读取锁等待、Request/Run/Message 归属，Redis 客户端读取真实 ARQ job。两轮合计 386 个请求保持 FIFO 与唯一 Run，前驱终态由夹具显式模拟，不能据此声称模型执行成功。

原实现负控产生 334 个连接、320 个锁等待，每进程借出峰值 160；持锁期间扫描不能结束。修改后每进程借出峰值 1，总连接峰值 16，锁等待 0，持锁扫描约 0.4 秒结束。16 包含三个进程的业务连接及各四个默认 checkpoint 连接，另有一个独立观察连接。该样本证明恢复扫描连接有界，不代表生产容量或吞吐 SLA。

既有失败 scope 隔离 unit 增加峰值断言，原全量 gather 按预期失败。Runtime workflow 直接执行恢复扫描脚本，失败阻断 job。远端 CI 与部署结果由 PR 单独记录；不访问生产或付费模型。

真实 worker 链路复用 `run_execution_retry.sh`：停止 worker 后通过 HTTP 提交 pending Run，删除该合成 Run 的 Redis job 模拟提交后的发布丢失，重启 worker，由启动恢复扫描重新派发。独立回读最终消息、唯一 completed Attempt 与 MCP 副作用文件；同场保留已执行但响应未知时不重放的负向边界。该脚本与双进程 integration 均接入 Runtime gate。
