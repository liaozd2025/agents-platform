# 索引部分失败保留真实统计并报告失败

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/knowledge/implementations/milvus.py

## 问题

201 块索引在最后一块嵌入失败时，前 200 块已落库，文件统计仍为 0；手动和待处理任务捕获逐文件错误后正常返回，Task 被记为 success。

## 决策

失败或取消后从 PostgreSQL 已持久化块重算文件统计，保持 error_indexing 与原处理 owner 条件；正在进行的双写先收敛再发布取消结果，避免线程在终态后迟到写入。取消信号只发送一次，清理期间继续续租直到 handler 退出，防止恢复器提前清 owner。手动与待处理批次保留既有失败明细，再按 ingest 的现有方式抛错，使 Task 显示 failed。重试沿用先清理旧索引再完整写入，不新增状态或补偿队列。

## 替代方案

失败后删除所有已成功块增加存储副作用，并不能保证外部故障时补偿成功；保留已提交部分并明确错误和实际统计更直接。逐块恢复游标需要新协议，现有完整重试足够。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
| --- | --- | --- | --- | --- | --- |
| 部分索引失败的统计和 Task 状态真实 | 200 块被记为 0、Task success | 索引执行器与知识 Task handler | 真实 HTTP/worker/PG/Milvus，201 块指定末块失败 | 原 handler 两入口不抛错；恢复旧统计时 200 != 0 | Passed |
| 重试和取消保持内容与 owner 一致 | 重复块、遗漏及迟到写入 | 双写等待与文件 owner 条件 | 同一 worker 重试、取消与健康索引 | 双写线程屏障及真实 PostgreSQL 跨租约取消，恢复旧 heartbeat 提前发布 failed | Passed |

## 后果

统计来自 PostgreSQL，不把多存储写入宣称为原子事务。进程崩溃、存储长时不可用仍需既有恢复流程。仅本地合成数据，不访问生产或付费模型。

## 验证

`backend/test/e2e/knowledge/run_index.sh <API_IMAGE> <PROVISIONER_IMAGE>`：一次性真实 HTTP/worker/PostgreSQL/Milvus/MinIO 与本地确定性 embedding；201 块末块失败、手动及待处理失败、健康与重复重试、跨库拒绝、末块取消后重试均通过，独立回读 Task、块内容和数量、文件统计及 owner。双写取消 unit 覆盖线程迟到写入；真实 PostgreSQL 使用缩短的 lease/heartbeat 周期，验证取消排空跨多个租约仍保持 owner，恢复旧逻辑时恢复器提前发布 failed。原 handler 两入口负控及恢复原统计的真实存储负控均在目标原因失败。生产与付费 provider 未运行。
