# PI 文档直接回填与执行终态修复

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/agents/backends/sandbox/backend.py

## 问题

DOCX 回填被拆成结构、坐标、渲染与重复检查；文本模型无法目视验收，仍不断生成预览。锁定沙盒服务忽略客户端的超时参数，PTY 在输出重定向后返回无输出超时，平台提前判失败但未停止真实进程。父任务随后重新委派，产生重复副本和延迟交付。

## 决策

主与 PI 的提示延续[阶段委派决定](2026-09-17-pi-delegation-performance.md)，要求主智能体先给出简短计划，并复用已验证结构批量加工和回读。文档验收方法及共享执行范围由[阶段交接与执行边界](2026-09-18-pi-workflow-boundaries.md)拥有；通用执行器不再覆盖 Skill 的目视验收要求。下述 DOCX 样本只证明该特定模板可以直接回填，不构成通用验收方法。

流式进程以包装命令的退出码文件为完成证据。沙盒无输出超时只表示观察暂停，在执行期限内继续读取真实输出和退出码；期限耗尽或取消时确认停止进程，再删除捕获文件。未确认执行或清理失败后的委派阻断适用于共享执行树；幂等重放仍返回原任务。

## 替代方案

- 更换 DOCX 库：实际回填工具耗时很小，不能解决模型循环和错误终态。
- 只延长沙盒参数：锁定服务不接受这些参数，不能形成有效期限。
- 修改基础镜像服务：增加私有补丁；已有退出码文件和本地期限足以拥有进程完成语义。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 结果 |
|---|---|---|---|---|---|
| 无输出超时不会提前结束仍在执行的任务 | 120 秒返回失败且继续写文件 | sandbox backend | unit 与真实 Local sandbox 长命令 E2E | 旧分支在 no_change_timeout 用例提前返回失败 | 通过；125 秒真实命令正常完成 |
| 取消与期限耗尽后停止文件写入 | 后台旧任务继续产生副作用 | sandbox backend | 真实 Local sandbox 文件与进程回读 | 无退出码时跳过停止 | 通过；取消与到期后进程消失、文件停止增长 |
| 执行未确认时禁止本轮重复委派 | 再次写入同一项目 | PI start service | unit 与真实 PostgreSQL 验证 | 删除拒绝分支时 3 项用例未抛出预期错误 | 通过；真实 worker / PostgreSQL 拒绝两个错误类型的新委派 |
| DOCX 批量回填并通过文本检查后交付 | 渲染和模型多轮分析阻塞返回 | parent middleware / PI Runner | Runner 与实际模板探针 | 原 Skill 包含目视步骤，执行要求覆盖该步骤 | 通过；一次委派交付 DOCX 与文本校验记录 |

## 后果

文本检查不证明视觉排版正确，交付说明明确未做目视验收。模板探针验证了 PI 阶段，未覆盖父模型的计划展示和知识库取证全流程；模型仍可能拆分脚本，不能保证所有输入都固定调用次数。当前变更不发布服务、不修改已完成或失败的用户 Run。

相关命令使用独立 Compose 项目、数据库与沙盒，不共享正在运行的开发数据：

```bash
docker compose -f <隔离环境配置> exec -T api uv run --no-sync --group test pytest test/unit/backends/test_sandbox_backends.py test/unit/services/test_pi_sandbox_run_service.py test/unit/services/test_pi_execution_service.py test/unit/middlewares/test_pi_sandbox_middleware.py -q -p no:cacheprovider
docker compose -f <隔离环境配置> exec -T api uv run --no-sync --group test pytest test/e2e/test_pi_local_tracer.py -k "silent_pty_timeout or cancel_stops_command or delivers_stdout or assembled_path" -q -p no:cacheprovider
```

相关 unit 为 163 passed；上述真实沙盒与 worker E2E 分批合计 6 passed。锁定镜像中 Node Runner 测试 19 passed。工程契约及其 61 项测试通过；文档构建通过，存在 VitePress/Rolldown 插件警告。

实际模板探针使用原医院模板与原入院报告 Skill、同一 DeepSeek V4.1 Flash 模型，以及预先给定的模拟字段，不调用知识库。一次 PI 委派完成并清理共 77.3 秒，10 次工具调用；交付 DOCX 与文本校验记录。独立 XML 回读确认指定值、表格/行/单元格及合并结构，替换区与人工签批区保持原样，final 接收时源模板字节未改动；轨迹没有执行渲染、预览或 Minimax DOCX 工具。该样本证明直接回填可完成，不代表完整业务耗时或固定加速比例。
