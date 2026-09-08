# 聊天沙盒按需创建

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/services/chat_service.py

## 问题

普通问候在模型执行前创建沙盒，回复持久化后又等待沙盒销毁，用户因此承受与聊天无关的启动与收尾延迟。

## 决策

Chat 与 Resume 直接进入 Agent。PI 工具首次执行时创建或复用执行树的沙盒，保留现有容量等待与取消行为；CompositeBackend、展示产物、OCR、知识库下载与 Skill 安装在实际文件操作时按需创建。Skill 投影仍由构图入口同步。根 Run 继续拥有清理与终态发布顺序。

## 替代方案

保持预创建会让普通聊天继续承担成本；按问候词分类不能覆盖普通聊天；提前发布终态会改变清理隔离边界。选择使用真实工具执行触发创建。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 普通聊天与恢复可完成且不创建沙盒 | 预创建残留 | chat_service | chat stream unit；`TEST_CHAT_MODEL` 指定模型的 `test/e2e/test_chat_lazy_sandbox.py` | 沙盒创建入口抛错时聊天仍完成；每个 SSE 事件回读实例不存在 | Passed |
| 首次 PI 工具能创建沙盒并产生产物 | 仍只 discover 父实例 | pi_execution_service | `test/e2e/test_pi_local_tracer.py -k assembled_path` | 无预建父沙盒，回读子 Run 与产物 | Passed |
| 实际文件操作可按需创建 | 长结果落盘失败 | composite backend | `test/unit/backends/test_sandbox_backends.py` | 无现有连接，真实 backend 取得连接并执行 | Passed |

使用 `docker compose exec -T api uv run --no-sync --group test pytest` 运行相关用例。真实模型多轮问候均完成；端到端约 2–4 秒发出正文，正文到结束事件约 0.01 秒；该测量不是所有供应商的延迟承诺。同期 Docker container create 事件为空。非 slow 单元测试 1882 通过、45 跳过，文档构建通过。工程验证器存在三份未修改的 2026-09-04 decision 的五项既有格式错误，不能记为通过；验证器自身 61 项单元测试通过。

## 后果

容量不足在实际工具使用时暴露。PI 保留异步有界等待；同步文件操作立即报告容量错误，避免在线程池任务取消后继续等待并创建实例。文件读写不会把容量不足伪装成权限或路径错误。用户身份、Workdir、generation 与根 Run 清理约束保持原有 Owner；不引入缓存或独立生命周期。
