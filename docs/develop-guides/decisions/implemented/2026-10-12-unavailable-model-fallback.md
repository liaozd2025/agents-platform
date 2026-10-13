# 模型下架后聊天页自动回退到可用模型

状态：implemented
类型：bug-fix
Owner：web/src/components/ModelSelectorComponent.vue

## 问题

供应商把模型从 `enabled_models` 移除后，模型不再出现在 `ModelCache.rebuild` 的结果里，`/api/model-providers/models/v2` 不再返回它。平台有三处不知道这件事：

1. **前端**：`resolveConversationModel` 是纯 `||` 链，从不与可用列表比对；`displayModelText` 只做字符串切割，因此界面照常显示该模型名，用户看不出异常。
2. **读取历史会话**：历史状态通过 PostgreSQL checkpoint 直接读取，与模型构图解耦；会话绑定的失效模型不得影响快照与消息读取。
3. **运行时默认模型**：`agents/context.py` 只在 `model` 为空时才回落 `default_model`。`default_model` 本身被下架后，回落得到的仍是失效 spec，同样抛异常。

发送路径的 422（`agent_run_service.py`）是有意设计的最后防线，保持不变。本记录处理的是前两处「用户无法继续使用」的问题。

本仓库的 `2026-09-22-chat-model-preference.md` 已把「后端在发送时拒绝」记为「用户改选一次即可恢复」的已知代价；本次不改变那个取舍方向。

## 决策

前端根据可用模型目录改选并提示用户；运行时准备在默认模型失效时选择可用对话模型并记录日志；历史状态直接读取 checkpoint，不解析模型。

### 后端

- `agents/context.py` 新增 `_is_model_spec_resolvable(spec)`：空值、缓存中查不到、非 chat 类型都视为不可解析；新增 `resolve_available_default_chat_model(db)`：优先用配置的 `default_model`，不可用时取可用 chat 列表首项，全不可用返回空串（不塞非法值）。`prepare_agent_runtime_context` 校验可解析性；非空模型已绑定 Run 时失效必须显式失败，无 Run 的上下文或空值可以回落默认。
- `get_agent_state_view` 保留直接读取 checkpoint 的链路，读取会话不构建执行图、不解析会话或子智能体模型。真实 HTTP 测试验证绑定已下架模型时仍能读取持久快照。

### 前端

- `web/src/utils/conversationModel.js` 新增 `isModelSpecAvailable` / `resolveFallbackModelSpec` / `extractModelName`。`extractModelName` 从选择器内部提取复用，行为不变。
- `ModelSelectorComponent.vue` 在 `fetchV2Models` 成功回调里调用 `applyModelListSideEffects()`，统一执行「空选择兜底首项」与「失效模型回退」——这是唯一同时持有当前 spec 与权威列表的位置。失效判定以 `hasFetchedV2Models` 为唯一前提：列表未加载完成或加载失败时不判定，避免首屏竞态和网络抖动误换用户模型。
- 回退由独立 prop `fallback-when-unavailable` 控制，**不复用 `auto-select-first`**。后者表达「没有选择时兜底」，前者表达「已有选择但已失效时兜底」。聊天页必须同时开启两者；设置页两者都不开——失效配置必须原样展示，管理员才能看出自己配的模型已下架。
- 回退结果带 `{ autoSelected: true, unavailableSpec }`。`autoSelected` 使 `handleModelSelect` 不把兜底结果写入用户偏好（否则失效模型名被永久固化，每次新建会话重复回退）；`unavailableSpec` 让聊天页用 `message.warning` 告知原模型与替代模型，不静默换。
- `recoveredUnavailableSelections` 记录已回退过的「会话范围 + 失效 spec」，保证同一会话内重复拉列表不会让用户选择来回抖动。键必须包含会话范围：切换会话时组件实例复用，两个会话都绑定同一个已下架模型时 `model_spec` 完全相同，只按 spec 去重会让第二个会话不再回退。
- 同时监听会话 id 与当前模型变化，复用已加载目录重新判定；会话模型 metadata 异步到达时也会回退。输入变化后清理回退去重，重新打开同一会话仍可恢复。

## 替代方案

- **后端在 spec 失效时静默回落到 `default_model`**：会让用户以为在用 A 实际用 B。`AGENTS.md` 要求「不把未就绪伪装为成功」，本仓库用户多在医疗与知识库场景，模型差异直接影响回答质量，不采用。
- **复用 `autoSelectFirst` 作为场景开关**：实现更省一个 prop，但语义错位且**静默失效**——聊天页传 `!currentModelSpec`，模型下架后它为 `false`，列表加载与回退都不触发，缺陷看起来已修实际未修。
- **在失效判定里加「请求进行中」守卫**（`loadingV2Models`）：语义上想避免请求中途判定，但判定本身就发生在成功回调里，而该标记要到 `finally` 才复位，回调期间仍为真，把本该成立的判定挡掉。只保留 `hasFetchedV2Models` 一个判据。
- **对所有调用方统一回退**：会让设置页、知识图谱、定时任务、评测弹窗的已失效配置被自动改写，其中多处是管理员显式配置的结果。
- **改 `options.py::resolve` 校验 `default_model`**：能让管理员保存配置时得到反馈，但该函数是纯同步解析，引入模型缓存依赖会把配置层与运行时缓存耦合；且默认值失效属于管理员配置问题，本次在运行时上下文层解决即可。

## 后果

- 模型下架后聊天页自动切到列表首项并给出一次 warning，历史会话可以正常打开并继续对话。
- 设置页行为不变：失效配置仍原样展示。
- 系统默认对话模型自身被下架时，未绑定 Run 的上下文回落到可用列表首项。已接收 Run 的非空模型在排队期间下架时显式失败，用户改选后重试，避免标签显示 A 而实际使用 B。
- 会话与 Run 中记录的 `model_spec` 保持原值（读取路径不回写），因此模型重新上架后用户切回该模型的行为不受影响。
- 后端 422 语义与 `test_agent_run_service.py` 的既有断言不变。

## 验证

- 后端回退与上下文权限测试在隔离 Compose API 容器执行，共 36 项通过；覆盖空值、下架默认模型、有效选择保留与已绑定 Run 拒绝换模型。
- 前端模型绑定与真实组件加载测试在隔离 Compose Web 容器执行，共 25 项通过；覆盖异步 metadata、跨会话、再次打开、重复目录加载、失败重试与多模型类型。
- 真实浏览器连接独立 API、PostgreSQL 和 Vite 页面，打开两个绑定 `pr158-test:removed` 的合成会话并重新打开首个会话，模型均显示 `pr158-test:available`；截图证明最终选择。
- 真实 HTTP checkpoint 测试覆盖失效模型绑定、持久消息与快照、跨用户隔离、未知及删除线程。
- 前端 lint、build 与 docs build 通过。完整前端测试通过；后端完整测试的两项 Compose 资源限制检查失败，相关配置与测试和 main 一致。工程 verifier 单元测试 64 项通过；扫描因 main 现有 AGENTS.md 字符预算超限而失败。
- 未验证：外部供应商真实调用、worker 压缩与子 Agent 全链路；运行绑定拒绝替换由上下文负向测试覆盖。
