# PI 交付目录优先级

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/pi_runner/runner.mjs

## 问题

PI Runner 只从一次运行独占的 `outputs/pi-runs/<run>` 目录生成 artifact 清单，但交付目录要求位于用户任务正文之前。用户任务包含旧的 `outputs/` 绝对路径时，模型可能按更靠后的路径保存最终文件，只把生成脚本留在独占目录，导致任务完成而状态面板只展示脚本。

## 决策

Runner 先提供原始用户任务，再在消息末尾声明本次独占交付目录，并明确它覆盖任务中冲突的输出路径。产物采集继续只读取该独占目录，不扩大扫描范围。

Local PI golden E2E 使用包含旧 `outputs/` 路径的任务模拟该冲突；只有最终文件仍写入独占目录并由 child Run 回读时才通过。

## 替代方案

- 扫描整个 Project `outputs/`：拒绝。多个会话可以共享 Workdir，扫描会把并发运行或已有文件错误归属给当前 Run。
- 在前端搜索或猜测聊天正文中的文件路径：拒绝。文本声明不是 artifact 事实，也绕过服务端摘要和路径校验。
- 全量改写任务中的 `outputs/` 字符串：拒绝。任务可能把该路径作为输入文件，机械替换会破坏真实读取目标。

## 后果

用户任务仍可读取和修改 Project 文件，但最终交付物以 Runner 分配的独占目录为准。修复依赖模型遵循消息末尾的明确约束；若真实模型仍发生越界，再基于实际写入集合增加确定性 gate，不提前扩大本次改动。

## 验证

- 冲突路径负向 E2E 在旧顺序下失败：child Run 状态为 `failed`。
- Local PI tracer E2E：`6 passed`；覆盖冲突路径、最终 ACK、取消清理、流式回调和运行时摘要。
- 后端 unit：`1709 passed, 40 skipped`；其中 PI 执行、child Run 与 middleware 聚焦测试 `25 passed`。
- 工程合同与合同测试：`63 decisions` 检查通过，`61 tests passed`。
- 文档构建通过；保留既有 Rolldown 插件与大 chunk 警告。
