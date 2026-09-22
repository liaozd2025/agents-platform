# 自定义智能体的系统提示词优先于默认身份

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/agents/buildin/chatbot/prompt.py

## 问题

在项目页面创建智能体、后端选择「智能助手」并配置系统提示词后，对话询问「你是谁」仍返回「我是九典AI助手」，自定义人格在所有身份类问题上失效。

根因是身份占位与用户设定同时注入。`build_prompt_with_context` 将系统提示词按 `当前日期` → `默认身份` → `平台约束` → `文件系统约束` → `context.system_prompt` 顺序拼接。默认身份包含一条排他性短路指令（"当用户问你是谁时，只回答'我是九典AI助手'，不要补充其他内容"），条件窄且明确禁止补充；用户自定义提示词只是尾部追加、未声明优先级。两条同条件指令冲突时模型优先遵从前者。

`buildin/subagent/graph.py` 复用同一构造函数，子智能体存在同一行为。

首版修复按 `system_prompt` 的最终内容判定「是否自定义」，随即暴露第二个缺陷：`build_agent_input_context` 会把用户工作区的 `agents/AGENTS.md`、`agents/USER.md` 追加进 `system_prompt`（实测该模板内容为 128 字符）。未配置提示词的智能体因此也被判为「已自定义」，默认身份不再注入，模型没有任何身份声明，只能用平台提示词里的描述泛化自称「我是一个 AI 助手」。身份判定不能使用被追加后的值。

## 决策

把系统提示词拆分为「平台层」与「产品默认身份层」，明确覆盖语义：

- `PLATFORM_PROMPT`（回答规范、内部执行约束、`ask_user_question`、风格规范）始终注入，不可覆盖。
- `DEFAULT_IDENTITY_PROMPT` 属单值语义，仅在 Agent 未配置自定义系统提示词时注入。
- 已配置自定义提示词时改为注入 `CUSTOM_IDENTITY_NOTICE`（只声明身份归属，不再声明具体身份），并把自定义内容放入 `<| 用户自定义设定:最高优先级 |>` 区块置于末尾。
- 判定依据是 **Agent 自身的配置值**，由 `yuxi/agents/context.py` 拥有：`DEFAULT_SYSTEM_PROMPT_PLACEHOLDER` 定义默认占位值，`is_custom_system_prompt()` 在空值、纯空白与占位值三种情况下返回未配置。`build_agent_input_context` 在**追加工作区内容之前**算出该结论，写入 `BaseContext.system_prompt_is_custom` 运行时字段（`configurable=False`、`hide=True`）供 prompt 构造消费。
- `build_prompt_with_context` 优先读取该运行时标志；标志缺失（例如新增的、直接构造 Context 而不经 `build_agent_input_context` 的调用路径）时回退到按内容判定。该兜底**不等价**于主路径：它只能看到追加工作区内容之后的值，在「未配置 + 存在工作区 `AGENTS.md`/`USER.md`」时会判为已自定义。生产链路始终写入该标志，不会走到兜底分支。
- 工作区 `AGENTS.md`/`USER.md` 仍作为补充设定拼在末尾，但不再影响身份归属；自定义模式下它们与用户设定同处「最高优先级」区块内。
- 前端同步修复两处：`AgentEditModal.vue` 的创建分支补上 `config_json`；`stores/agent.js` 的 `resetAgentConfig()` 改为同时清空 `agentConfig` 与 `originalAgentConfig`，使新建弹窗不再预填上一个智能体的配置，且用户填写即被识别为变更。

未配置自定义提示词的路径输出与改造前逐字一致，默认智能体仍自称九典AI助手。

## 替代方案

- 只在默认身份句后追加「若自定义设定定义了身份则以自定义为准」：不改拼接逻辑，但冲突仍由模型隐式权衡，遇到强冲突提示词仍会失效。
- 在聊天服务中拦截身份类问题返回定制消息：为身份文案增加特殊请求分支，绕过正常 Agent 回复链路，且无法覆盖任意自定义身份。
- 前端不展示或隐藏系统提示词字段：不改变模型可见输入，问题依旧。

## 后果

- 新智能体配置系统提示词后，身份、人格与话术由自定义设定接管；平台安全与执行约束不受影响。
- 内置智能体（`WEB_SEARCH`、`DEEP_RESEARCH` 等自带 `system_prompt`）不再叠加默认身份。这是行为变更，方向与预期一致：研究类智能体不应自称通用问答助手。
- 仓库内置的默认「智能助手」模板 `config_json` 为 `{"context": {}}`，其 `system_prompt` 为空，行为不变。
- 未配置自定义提示词时，`system_prompt` 仍按原逻辑拼在末尾（可能是 `BaseContext` 默认占位值，也可能已被工作区 `AGENTS.md`/`USER.md` 内容覆盖），属改造前既有行为，本次不扩大范围。
- `BaseContext` 新增运行时字段 `system_prompt_is_custom`，标记为 `hide`、`configurable=False`，不进入用户可配置面，前端 schema 驱动的表单不会渲染它。
- 拆分了原 `PROMPT` 常量；该常量无外部引用，`SOURCE_CITE_PROMPT` 与 `TODO_MID_PROMPT` 未改动。
- 身份判定与占位值常量从 `chatbot/prompt.py` 迁到 `yuxi/agents/context.py`：身份归属是 Context 级事实，与 `system_prompt` 字段同源，不应由单一后端拥有。
- 已知边界：若用户把系统提示词恰好写成占位值 `You are a helpful assistant.`，会被判为「未配置」并注入产品默认身份。该值无业务含义，概率极低，接受。
- 未覆盖：新建智能体弹窗的字段集合仍来自上一个选中智能体的 `configurable_items`（`openCreate` 不改变 `selectedAgent`）。清空配置后不再残留数值，但字段集合与目标 backend 的 schema 可能不一致；属既有问题，本次不扩大范围。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 未配置自定义提示词时输出与改造前逐字一致 | 默认智能体身份或平台约束发生变化 | `backend/package/yuxi/agents/buildin/chatbot/prompt.py` | AST 提取 `HEAD:PROMPT` 与新版 `DEFAULT_IDENTITY_PROMPT + PLATFORM_PROMPT` 比对：`True` | — | Passed |
| 配置自定义提示词后按自定义身份回答 | 询问身份仍回默认品牌 | 同上 | 真实 DB 配置 + 真实模型（`alibaba-cn:qwen3.7-max`）：id=1「智能助手」配 1955 字产品经理提示词 → 回答「我是一位拥有10年以上经验的资深AI产品经理与智能体架构师…」 | 若默认身份改回无条件注入，`test_custom_system_prompt_takes_over_agent_identity` 因残留身份锁定句失败（已实测 1 failed） | Passed |
| 工作区追加内容不污染身份判定 | 未配置提示词的智能体丢失默认身份，泛化自称「我是一个 AI 助手」 | `backend/package/yuxi/agents/context.py` | 真实链路：`build_agent_input_context` + 真实 `agents/AGENTS.md`/`USER.md`（128 字符）→ 标志 `False` → 仍注入默认身份；真实模型答「我是九典AI助手」 | 若改回按 `system_prompt` 内容判定，`test_workspace_appended_prompt_does_not_suppress_default_identity` 失败（已实测 1 failed） | Passed |
| 默认占位值与空值不被误判为自定义 | 全部智能体丢失默认身份 | `yuxi/agents/context.py` | `test_is_custom_system_prompt_excludes_default_placeholder` | — | Passed |
| 平台约束与文件系统约束不因自定义而丢失 | 自定义提示词挤掉安全约束 | `buildin/chatbot/prompt.py` | `test_custom_system_prompt_takes_over_agent_identity` 断言 `ask_user_question` 与 workdir 边界仍在 | — | Passed |
| 标志缺失时的兜底分支行为被固定 | 兜底分支被无声删除或改写 | `buildin/chatbot/prompt.py` | `test_missing_runtime_flag_falls_back_to_content_check` 固定兜底判定走自定义分支的行为 | — | Passed |
| 新增智能体时提交模型配置 | 「新增智能体」弹窗填写的配置被静默丢弃 | `web/src/components/model-management/AgentEditModal.vue`、`web/src/stores/agent.js` | 代码审查确认 `payload.config_json` 已提到新增/编辑两条分支之外；`resetAgentConfig()` 清空基线后，用户填写即被视为变更（独立 Reviewer 指出的残留基线问题已随本次修复消除） | — | Passed（代码级审查） |
| 子智能体（复用同一构造函数）行为一致 | 父子智能体身份规则分裂 | `backend/package/yuxi/agents/buildin/subagent/graph.py` | 复用 `build_prompt_with_context`，随本次改动一并生效 | — | 随同生效，未单独做交互验证 |

执行命令与结果：

- `docker exec test-api-1 sh -c "cd /app && python -m pytest test/unit/agents -q"`：**147 passed**。
- `docker compose exec -T api python -m pytest test/unit/agents/test_chatbot_prompt.py -q`：8 passed（两次负向实验均为 1 failed，目标用例有效）。
- `git diff --check`：通过。
- 前端改动经 `<script setup>` / store 提取后 `node --check` 语法校验（`web/node_modules` 缺失，lint 与单测无法执行）。
- 未执行项：页面端新建智能体后真实对话回归（需 UI 操作）、子智能体的独立交互验证。

## 独立语义 Review

由不继承开发上下文的独立 Reviewer Agent 完成（2026-09-21），覆盖完整 diff、8 项针对性问题与测试范围：

- 阻断问题：无。
- 重要：`openCreate()` 的残留基线会削弱"新增时提交配置"的修复（`changedAgentConfig` 与基线相同则不提交）→ 已随本次一并修复（`resetAgentConfig()` 清空基线）。
- 次要：占位值边界（用户恰好写 `You are a helpful assistant.` 判为未配置，接受）；决策记录"两条路径行为一致"表述不准 → 已修正为"安全兜底、非等价路径"；工作区内容并入最高优先级区块的语义说明 → 已补充。
- 未发现问题：身份互斥无遗漏注入路径；标志计算时机正确；未配置场景输出与改造前逐字一致；测试无恒真断言。

## 关系

部分取代 [默认聊天助手使用九典身份](2026-08-20-default-assistant-identity.md)：该记录确立的九典默认身份仍然有效，但"拼接顺序保持不变"的结论已被本次覆盖语义取代。
