# PI 沙箱 Web 协作体验实施规格

本规格面向实施者与 Reviewer，承接 PI 沙箱代码走查及用户的 implement-spec 请求。目标是让 Web 中的 PI 任务可靠执行、正确交付、持续反馈并续接项目上下文。最终交付为一个完成验证和独立 Review 的 PR。

## 边界与验收

保留 LangGraph 主会话、Request FIFO、AgentRun/Attempt、PostgreSQL final ACK、Redis SSE 与 Project Workdir 的现有 Owner。用户已有的沙箱 stdout 增量修改纳入本规格基线；聊天默认模型等无关未提交改动留在原工作树。

默认部署以外部模型 API、64GB 单机为容量起点。动态沙箱必须具有可配置内存、CPU、PID、tmpfs 上限；worker 默认并发从当前隐式值收敛为 4。资源参数是可校准预算，不宣称已完成生产容量验收。供给层须拒绝超过容量的创建并暴露明确原因；等候复用现有请求/Run 调度语义，不另建任务系统。

取消必须覆盖业务 cancel_event 与外层 coroutine cancellation，确认内部执行停止后才能清理；final ACK 已提交的结果继续保留。沙箱流式输出须有总量上限、完整 JSONL 行语义及取消清理。

PI 工作目录与交付收集范围须明确分离。依赖、缓存和辅助文件不得被无差别登记为最终交付物；大于 8MiB 的普通合法文件须能交付。所有交付仍绑定当前 attempt，目录逃逸、符号链接、大小/摘要冲突必须在文件边界拒绝。只读任务允许没有交付物，不能从共享目录猜测本次产物。

同一用户、Project、根会话的后续 PI 任务可以续接已保存 session；不同会话不得交叉读取 session，活动 session 串行访问。续接来源须经服务端归属验证，不能信任模型提供的历史路径。加载获准的当前 Project 指令，保留 Skill 权限快照。模型能力沿用现有元数据，真实 token usage 归属本次 child Run。

PI 正文增量、命令中间输出经既有 SSE 与消息组件呈现，增量事件合并且不逐 token 重写数据库历史。运行中引导须与现有主会话 steer 请求的唯一消费语义兼容；只接受一次输入，不让当前 PI 和下一 Run 重复执行同一要求。取消和引导必须通过真实链路验证。

审批默认沿用现有任务级授权与 always_trust 两种模式，并在用户批准前说明沙箱命令和用户工作区访问范围。逐命令审批仍待用户对体验选项的回复；若明确选择则补入对应执行与 UI 验收，不能仅靠提示词模拟授权边界。

生产模板须能选择与源码匹配的 PI 镜像，构建/启动步骤包含 Runner 一致性验证；不发布不存在的镜像标签。常规 CI 使用受控模型上游经过真实 runTask 分支，验证 Request → worker → PI → SSE → 文件/数据库，不依赖计费模型或腾讯云。

非目标：实现腾讯云 Issue #48/#53、执行生产部署或镜像对外发布、修改数据库/密钥权限、引入 Kubernetes/暖池、改动同一用户内 Project 隔离承诺、未经测量重构事件存储。既有 patch 不能被误称为项目源码的可回滚 diff。

## 任务图

任务依赖以本表为准；编号仅用于本次工作分配，不作为中央工程主张体系。

| Ticket | 内容与主要 Owner | 前置 | 完成检查 |
| --- | --- | --- | --- |
| T1 | 取消收敛：pi_execution_service 与流式 backend | 无 | 外层取消、业务取消、ACK 竞态和真实命令停止 |
| T2 | 资源预算与镜像部署：provisioner、Compose、WorkerSettings、配置文档 | 无 | 非法配置/超容量负向、真实 Docker 资源回读、部署配置验证 |
| T3 | 工作目录与交付契约：PI runner、adapter 文件回读 | T1 | 9MiB 合法交付、依赖不入 manifest、路径/超限拒绝、真实文件回读 |
| T4 | 会话/项目上下文/模型能力/用量：PI session、worker、Run metadata | T3 | 两轮续接、跨会话拒绝、项目指令、usage 同 Run 归属 |
| T5 | 增量交互与引导：runner、worker、SSE、Web | T1、T4 | 首段正文/命令早于终态、引导一次消费、刷新与最终状态一致 |
| T6 | 真实 runTask 验收、CI 与产品文档 | T2、T3、T4、T5 | HTTP/worker/PI/文件/数据库 E2E、Web lint/unit/build/浏览器、全量必要 gate |

各实现任务在独立 worktree 完成；每次代码提交前由全新 Reviewer 审查需求、diff、测试和规范。集成后再进行完整 Standards/Spec 双轴审查。生产负载与真实供应商校准另列未验证范围，不能用 deterministic E2E 代替。
