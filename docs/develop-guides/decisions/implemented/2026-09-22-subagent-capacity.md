# 父任务等待时推进子任务

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/services/run_worker.py

## 问题

父 Run 等待子 Run 时继续占用 ARQ 执行位置。四个位置都由等待父任务占据时，已提交并入队的子任务无法领取，父子链停止推进。#93 的真实样本在 35.04 秒内保持四父 running、四子 pending 且零 Attempt；取消一个父后，其余三组才完成。

## 决策

父 worker 执行作用域在等待自身子 Run 时启动受管理的子执行 Task，复用 PostgreSQL lease、持久 Attempt 预算和现有执行入口。每父同时最多推进一个子 Run；已由其他执行者领取的子任务仍等待原 Owner。任务只在父 scope 内存活，父终态通过持久取消通知子 Owner，退出时等待关闭，不能重复取消并打断终态写入。

子执行使用空 Context，从持久 Run 恢复身份与输入。不能继承父 LangGraph 的事件流、checkpoint 或工具审计上下文。普通 HTTP 等待没有启动执行的能力。

等待仍通过原有 SSE 上限结束；等待超时不取消子任务，外部取消必须向调用者传播。父之后继续工作时，子可继续执行，直到子本身终结或父运行结束。子 Task 的领取前错误经事件流检查传给等待方，不隐藏到父退出；已结束 Task 可在再次等待时重新检查持久状态。

## 替代方案

单纯增加 worker 或槽数只推迟饱和。独立子队列与常驻 worker 增加部署要求。释放父槽但继续接收更多父任务会放大等待内存，仍可能饥饿。直接在父工具内完整 await 子执行会绕过等待超时，并把 ARQ 顶层吞取消语义带入父调用栈，故未采用。

## 后果

ARQ 配置仍为四槽；每个父至多额外推进一个子执行，不新增 worker。父等待期间仍保留内存与 lease。若等待超时后父继续计算，实际活跃 Run 可能达到槽数的两倍；这不是严格四个活跃 Run 的全局配额。当前产品不允许子任务再委派，因此深度有界。生产吞吐、内存峰值及跨副本公平性未据此承诺。

## 验证

`backend/test/e2e/runs/run_subagent_capacity.sh` 在一次性真实 HTTP、ARQ worker、PostgreSQL、Redis 与 Sandbox 中，以四父模型屏障占满四槽，覆盖同步 `task` 与异步 `subagent_await`。独立回读父子 Run 的最终消息、唯一 completed Attempt 和文件内容；取消长子运行后父子均 cancelled，无后续写文件或父模型继续，队列保持暂停，显式继续后派发正确 FIFO 请求。该脚本由 Runtime workflow 执行，失败阻断 job。

当前本地完整场景 34.95 秒通过，同步四组 15.632 秒、异步四组 12.780 秒。对照同一夹具与 `main@9d0e0d16` 原实现，93.17 秒仍为四父 running、四子 pending，按预期失败。两个独立 worker 容器的 cgroup 内存峰值分别约 397 MiB 与 493 MiB；这包含启动及场景执行，不是生产容量、性能提升比例或多副本压测结论。

全量 unit 为 2353 passed、53 skipped。最小负向检查覆盖非法 Run 类型、其他父、其他用户、非 pending Run、缺失 Run；等待上限、领取前错误传播及重新等待、上下文隔离与真实 SSE 取消分别有回归。恢复上下文继承时，真实文件场景会因工具审计无法关联子 Run 的模型消息而失败。执行中断不重放复用既有 `run_execution_retry.sh`，最终结果与运行命令记录在 PR。

所有 E2E 使用合成账号、内部网络和本地确定性模型。镜像提供既有依赖，源码挂载当前 checkout；远端 CI 与部署状态另行记录，不构成生产上线验证。
