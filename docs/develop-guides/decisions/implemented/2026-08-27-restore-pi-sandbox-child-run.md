# 恢复 PI Agent 沙箱 child Run

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/agents/middlewares/pi_sandbox.py

## 问题

官方主线兼容合并保留了 PI golden tracer，但没有包含产品分支中统一沙箱执行入口的两笔后续提交。普通 Chat 因而重新向模型暴露直接文件与命令工具；截断的 `write_file` 还会留下没有有效 `tool_use` 的孤立 `tool_result`，使后续 Anthropic 协议请求持续返回 400。

恢复 child Run 后仍有两个生命周期缺口：模型以 `stop_reason=max_tokens` 结束时，系统把未完成输出提交成 `completed`；PI runner 又只在进程退出后返回汇总 JSONL，既没有发布内部 `read`、`write`、`bash` 等工具事件，也没有把 sandbox child 投影到父 Agent state。于是聊天提前结束、待办状态与 Run 不一致，前端只能看到一个黑盒 `pi_sandbox`。

## 决策

普通 Chat、Resume 和 SubAgent 继续由 LangGraph 编排。模型侧所有用户沙箱读取、搜索、写入、编辑、OCR 和命令执行统一进入 `pi_sandbox`；每次调用持久化为 `run_type=sandbox` 的 PI child AgentRun，并在父 worker 槽位内复用父 Run 的 runtime scope 与 Project Workdir。child attempt 明确记录 `executor=pi`、PI runtime manifest、模型与已选 Skill 快照。

直接 `read_file` 只保留给可信共享或个人 Skill 根目录，其他直接沙箱工具在最终模型工具边界移除，并在 Tool execution 边界 fail-closed 重定向到 `pi_sandbox`，因此历史 checkpoint 也不能绕过。Agent 发起的远程 Skill 安装先通过 `pi_sandbox` 准备文件，再走既有确定性安装服务。

最终模型输入边界会移除畸形 tool-call，以及没有紧邻、完整结果组的 tool-call 和所有孤立 ToolMessage，并加入普通文本重试提示，避免污染 checkpoint 被重放给 provider。PI 模型任务在服务端和 Runner 两层都要求显式 API key 或受支持的认证请求头；`run_type=sandbox` 的非终态持久形状由 business schema v3 约束。

模型兼容中间件在同一模型节点内识别无有效工具调用的输出上限终止，先清除被截断的畸形调用，再携带已生成内容继续请求，最多续写三次；最终仍被截断则显式失败，不能提交伪完成。PI runner 对自身模型的 `length` 终止执行同样的有界续写，并订阅原生 `tool_execution_start` 与 `tool_execution_end`，通过 sandbox 异步命令轮询实时回传有界 JSONL。worker 将这些事件同时投递到 sandbox child 与直接父 Run 的既有通用工具消息协议；`pi_sandbox` 返回时把 child 摘要写入父 state，前端复用 `TaskTool` 和通用 CLI 工具卡。

PI final ACK 是结果提交栅栏：流式输出回调的异常必须原样传播；ACK 前的取消终止执行并删除 attempt scope，ACK 后并发到达的取消不能回滚已持久化结果，清理必须保留 outputs。

PI JSONL 使用独立的 32 MiB 总流上限，单事件仍限制为 16 KiB；截断工具参数保留文件路径供通用工具卡识别。模型 job 临时凭据由 runner 启动前后的双边 `finally` 清理，父工具事件目标从 creator Run 持久事实派生，不信任 child payload。

## 替代方案

- 所有普通消息直接交给 PI：会丢失 LangGraph 对话、checkpoint、Resume、Web/知识库工具和子智能体编排，拒绝。
- 只恢复前端 `executor=pi` 开关：API、SubAgent 和模型工具面仍可绕过统一边界，拒绝。
- 仅捕获 provider 400 后重试：checkpoint 中的非法消息关系仍存在，后续请求会重复失败，拒绝。
- 只提高各模型的 `max_tokens`：不同 provider 上限仍会变化，且不能纠正“截断等于完成”的错误语义，拒绝。
- 为 PI 新建专用事件协议和前端组件：会复制已有工具调用、结果和子运行状态机制，拒绝。

## 后果

- LangGraph 拥有对话和工具编排，PI Agent 唯一拥有用户沙箱任务执行，不保留双执行路径。
- PI child 与父 Run 共享沙箱生命周期；child 结束或失败不能释放父实例，只清理自己的确定性输出目录。
- 模型凭据只经沙箱临时文件传入 runner，并在解析前删除；Project outputs 与日志不保存凭据。
- `run_type=sandbox` 约束在 business schema v3 引入；当前 v1、v2、v3 部署必须按[数据库 Schema 迁移 Owner](./2026-08-24-versioned-schema-migration-owner.md)由唯一 storage migrator 升级至 v4，API 和 worker 在版本不匹配时 fail-closed。
- 一次纯文本模型节点最多产生四次 provider 请求；超过该上限的任务以失败收敛，避免无限续写和费用失控。
- PI runner 的一次最终回复同样最多产生四次模型请求；final ACK 与取消并发时以已提交结果为准。
- PI 工具参数与结果事件单项限制为 16 KiB；完整产物继续由既有 outputs/ref 契约承载，不把大输出塞入 Redis 事件。
- PI 内部工具轨迹复用普通工具卡，父会话和 child Run 使用同一事件内容，不维护第二套展示状态。
- 状态面板把父 LangGraph `todos` 明确标为“主 Agent 计划”，不把它当作 PI 总进度；同一 PI child thread 仍收敛为一项，但按真实 Run 展示总数及各状态计数。持久记录只按 `run_id` 去重；无 `run_id` 的流式占位才回退工具调用 ID，避免跨父 Run 复用工具 ID 时少算真实执行。

## 验证

- `pytest test/unit -m 'not slow'`（Compose API 容器）：1707 passed，40 skipped，8 warnings。
- 模型输入、PI 执行、取消清理与沙箱执行边界聚焦 unit：45 passed；覆盖畸形截断调用续写、非连续工具结果清理、有效工具调用不重放、流式回调异常传播、final ACK/取消竞态、长事件流独立上限、临时凭据补偿删除、creator 事件路由、cleanup orphan 取消收敛及历史直接工具 fail-closed。
- 真实 PostgreSQL integration 覆盖 sandbox 非终态约束、PI final envelope，以及当前 business v1/v2/v3→v4 迁移。
- `pytest test/e2e/test_pi_local_tracer.py`：6 passed；使用重建后的 `yuxi-pi-sandbox:0.7.2.beta2` 验证真实 provisioner、PI runner 的 `length` 截断续写、内部工具轨迹、产物 ACK、流式回调取消清理和无认证任务 fail-closed；其中组装用例覆盖 `pi_sandbox` middleware、持久化 child Run、worker、父 Run SSE、产物与 PostgreSQL 终态。
- Web：ESLint 通过，186 项 unit 通过，生产 build 通过；PI 的 `read`、`write`、`edit`、`bash` 复用已有通用 CLI 工具卡。
- Web 状态投影回归覆盖同一 PI thread 的 16 次运行（8 完成、8 失败）以及流式占位去重；父 Agent 计划与 PI/子智能体执行统计分区展示。
- Ruff check、Node syntax check、工程合同、文档构建、`git diff --check`：通过。
- 浏览器已在签入态历史真实会话验收主 Agent 计划说明、Agent 执行总数和 PI Run 计数；未重新发起计费 PI 任务覆盖运行中到失败的实时转场。
