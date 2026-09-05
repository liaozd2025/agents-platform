# PI 沙箱任务的可靠交付与持续协作

状态：proposed
类型：feature
Owner：backend/package/yuxi/services/pi_execution_service.py

## 问题

PI 已在现有 Run 体系中执行沙箱任务，但外层取消不能收敛全部异步任务，执行文件与交付文件混用，动态容器缺少资源限制。一次性 session 和仅工具首尾事件使 Web 用户难以继续任务或判断执行进展。

## 提案

在现有 runtime、Run、文件系统和 SSE Owner 内完成[实施规格](../../../enterprise/2026-09-05-pi-sandbox-web-experience.md)。保留当前 final ACK、用户工作区权限及主会话引导的唯一消费事实。沙箱限制从 64GB 单机预算起步并提供有限配置；session 续接从服务端已验证的当前会话事实派生；交付只收集本次明确产物。

## 替代方案

- 整个会话改用 PI：改变知识库、MCP、Resume 和 LangGraph 对话合同，不采用。
- 增大机器但不约束任务：无法避免单个失控任务争抢常驻服务资源，不采用。
- 用共享 outputs 扫描补齐交付：会混入并发和历史产物，不采用。
- 为进度建立独立任务/消息存储：与现有 Run 和 SSE 重叠，不采用。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
| --- | --- | --- | --- | --- | --- |
| 取消先结束执行再清理 | 外层取消留下后台任务 | pi_execution_service | 取消 unit 与真实 sandbox E2E | 在 asyncio.wait 时取消外层 | Not run |
| 容器不能突破配置资源与容量 | 并发创建超卖或无限资源 | provisioner | unit、Docker HostConfig 回读 | 非法参数与并发满额 | Not run |
| 正确交付当前任务文件 | 依赖/历史文件混入、合法大文件失败 | runner 与 Workdir | runTask E2E、文件摘要 | node_modules、9MiB、逃逸/超限 | Not run |
| 续接与用量保持会话/Run 归属 | 跨会话读取、历史用量累计冒充本次 | worker、PI session、repository | 两轮真实请求 E2E | 跨用户/会话、旧 session 引用 | Not run |
| 执行中反馈与引导真实生效 | 缓冲至终态、重复消费用户输入 | runner、SSE、请求队列 | HTTP E2E 与浏览器 | 延迟工具、运行中 steer、断线 | Not run |
| 部署和 CI 使用真实 PI runTask | golden 绕过生产分支 | Compose、workflow | 隔离 Compose 验收 | 缺失 Runner/错误 digest | Not run |

## 风险

会话续接会增加持久历史与上下文大小，必须保留范围校验和本次用量边界。引导不能将未执行的请求提前标记完成。资源默认值需要目标机器的代表性任务校准。开发验证使用独立运行槽位，避免干扰现有主工作树和腾讯云工作树；不进行生产状态变更。
