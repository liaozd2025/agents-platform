# PI 沙盒命令增量输出

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/agents/backends/sandbox/backend.py

## 问题

PI Runner 按行写入工具事件，但固定沙盒镜像的 Shell `async_mode=true` 和 SSE 都只在命令结束时返回累计输出。父 Run 因而在 PI 子任务结束前没有过程事件，终态刷新又会清除临时事件，前端看不到执行过程。

## 决策

`ProvisionerSandboxBackend.aexecute_stream` 使用 Shell 的短同步超时取得运行中 session，把命令 stdout/stderr 合并写入随机、权限为 0600 的沙盒临时文件。后端通过 HTTP Range 按字节偏移读取新增内容，再按完整 JSONL 行交给既有 output sink；终态补齐尾部，并从独立状态文件取得真实退出码。provisioner 保留 Content-Range 与 Accept-Ranges，后端兼容固定服务端的 416 响应格式。

固定沙盒可能在命令进程仍运行时报告 `completed`。此时必须先读取包装命令的退出码文件；404 表示完成尚未确认，继续按完整行消费增量输出，在原执行期限内等待。期限耗尽时先终止同一 session，再清理捕获文件，不能把缺失状态文件解释为成功。

现有 session 继续拥有超时和取消；输出超限、回调失败或取消均需确认同一 session 终止。停止无法确认或启动响应丢失时，保留捕获文件并报告 cleanup orphan；复用父沙箱的 child 由根 runtime 回收事实收敛清理，不能凭新建空 adapter 的关闭动作宣告成功。文件读取接口会剥除末尾换行，因此不能用其文本末尾判断命令是否已有完整输出。

PI 派生镜像将基础镜像的 `/shell/write` 修正为向当前 PTY 写入输入；基础实现会把输入当作新命令并错误终止运行状态。启动 inspector 对源码构建和预构建镜像都验证该补丁，固定注释控制仍须由当前 Runner 返回 ACK，HTTP 200 不作为输入送达的证据。

## 替代方案

- Bash offset API：客户端已包含，但固定沙盒服务端返回 404，拒绝。
- Shell SSE：端点返回 `text/event-stream`，但实测仍只在命令结束后返回累计输出，拒绝。
- 只持久化 PI 事件供历史回看：不能恢复运行中的实时反馈，不作为本缺陷修复。

## 后果

- PI 工具事件按完整 JSONL 行进入 child 与父 Run SSE，未换行的尾部在终态补齐。
- 每个流式命令产生两个仅位于沙盒 rootfs 的 0600 临时文件，确认执行结束后删除；无法确认停止时保留给清理收敛。
- worker 每 0.2 秒通过 Range 读取新增字节并查询 session 状态；未换行和跨块 UTF-8 字节留在有界缓冲，不重复下载已消费内容。

## 验证

- 伪完成负控在启动响应报告完成但退出码文件尚不存在时，仍交付后续完整输出及跨块 UTF-8；永久缺失状态文件时验证超时、进程终止和捕获清理。

- 原缺陷负控覆盖外层取消遗留执行、启动响应丢失、重复取消吞掉清理失败和 final 提交响应被取消后的产物误删。
- backend 与 PI service 聚焦 unit：`131 passed`，覆盖字节偏移、UTF-8、416、输出上限、取消和 ACK 竞态。
- 真实 Local PI 检查：`6 passed`；远端 release 门闩证明首行先于命令结束，PID 与文件字节回读证明取消后不再写入，golden 产物在 ACK 后保留。
- 最终 service 收拢修改后的真实 ACK/取消检查：`3 passed`；独立 Reviewer 复跑重复取消组合回归 `3 passed`。
- 全量后端 unit：`1788 passed, 44 skipped`；skip 未计为通过。一次无诊断的运行停顿后中断，追加诊断的完整运行通过，未将其描述为稳定性压测。
- 工程契约及 `61` 项契约测试、Ruff 与 diff 检查通过。完整 Web/引导集成结果见[持续协作决定](./2026-09-05-pi-sandbox-web-experience.md)。
