# 模型下架后聊天页自动回退到可用模型

状态：implemented
类型：bug-fix
Owner：web/src/components/ModelSelectorComponent.vue

## 问题

供应商把模型从 `enabled_models` 移除后，模型不再出现在 `ModelCache.rebuild` 的结果里，`/api/model-providers/models/v2` 不再返回它。平台有三处不知道这件事：

1. **前端**：`resolveConversationModel` 是纯 `||` 链，从不与可用列表比对；`displayModelText` 只做字符串切割，因此界面照常显示该模型名，用户看不出异常。
2. **读取历史会话**：`GET /api/chat/thread/{id}/state` 把会话绑定的 `model_spec` 原样交给模型解析层，模型下架后抛 `Unknown model spec`，整个接口 500。**用户连历史会话都打不开**，比发送失败更严重。
3. **运行时默认模型**：`agents/context.py` 只在 `model` 为空时才回落 `default_model`。`default_model` 本身被下架后，回落得到的仍是失效 spec，同样抛异常。

发送路径的 422（`agent_run_service.py`）是有意设计的最后防线，保持不变。本记录处理的是前两处「用户无法继续使用」的问题。

本仓库的 `2026-09-22-chat-model-preference.md` 已把「后端在发送时拒绝」记为「用户改选一次即可恢复」的已知代价；本次不改变那个取舍方向。

## 决策

失效模型在**读取与运行时准备**阶段被识别并忽略，由前端改选可用模型并提示用户。后端不做静默替换。

### 后端

- `agents/context.py` 新增 `_is_model_spec_resolvable(spec)`：空值、缓存中查不到、非 chat 类型都视为不可解析；新增 `resolve_available_default_chat_model(db)`：优先用配置的 `default_model`，不可用时取可用 chat 列表首项，全不可用返回空串（不塞非法值）。`prepare_agent_runtime_context` 由「只判空」改为「判可解析性」。
- `services/chat_service.py` 新增 `_is_model_spec_usable(spec, source)`，用于 `get_agent_state_view`：会话绑定与子智能体继承的 `model_spec` 都先过这道判定，失效则不写入 `input_context["model"]`，交由下游回落。两处都记 warning 日志说明来源。
- 这两个函数都**只判定、不替换**：真正发给用户的是哪个模型由前端决定并提示，后端不替用户决定。

### 前端

- `web/src/utils/conversationModel.js` 新增 `isModelSpecAvailable` / `resolveFallbackModelSpec` / `extractModelName`。`extractModelName` 从选择器内部提取复用，行为不变。
- `ModelSelectorComponent.vue` 在 `fetchV2Models` 成功回调里调用 `applyModelListSideEffects()`，统一执行「空选择兜底首项」与「失效模型回退」——这是唯一同时持有当前 spec 与权威列表的位置。失效判定以 `hasFetchedV2Models` 为唯一前提：列表未加载完成或加载失败时不判定，避免首屏竞态和网络抖动误换用户模型。
- 回退由独立 prop `fallback-when-unavailable` 控制，**不复用 `auto-select-first`**。后者表达「没有选择时兜底」，前者表达「已有选择但已失效时兜底」。聊天页必须同时开启两者；设置页两者都不开——失效配置必须原样展示，管理员才能看出自己配的模型已下架。
- 回退结果带 `{ autoSelected: true, unavailableSpec }`。`autoSelected` 使 `handleModelSelect` 不把兜底结果写入用户偏好（否则失效模型名被永久固化，每次新建会话重复回退）；`unavailableSpec` 让聊天页用 `message.warning` 告知原模型与替代模型，不静默换。
- `recoveredUnavailableSelections` 记录已回退过的「会话范围 + 失效 spec」，保证同一会话内重复拉列表不会让用户选择来回抖动。键必须包含会话范围：切换会话时组件实例复用，两个会话都绑定同一个已下架模型时 `model_spec` 完全相同，只按 spec 去重会让第二个会话不再回退。
- 额外监听 `selection-scope`（会话 id）变化并复用已加载列表重新判定。切换会话不会重新请求模型列表（`fetchV2Models` 有并发去重，且列表内容未变），只靠 `fetchV2Models` 成功回调里的回退会漏掉后续会话。

## 替代方案

- **后端在 spec 失效时静默回落到 `default_model`**：会让用户以为在用 A 实际用 B。`AGENTS.md` 要求「不把未就绪伪装为成功」，本仓库用户多在医疗与知识库场景，模型差异直接影响回答质量，不采用。
- **复用 `autoSelectFirst` 作为场景开关**：实现更省一个 prop，但语义错位且**静默失效**——聊天页传 `!currentModelSpec`，模型下架后它为 `false`，列表加载与回退都不触发，缺陷看起来已修实际未修。
- **把失效判定写成 `computed`**：同样静默失效。回退在 `fetchV2Models` 成功回调内同步触发，computed 惰性求值，同一 tick 内读取会拿到依赖尚未失效的旧值。改为普通函数按当前值现算。
- **在失效判定里加「请求进行中」守卫**（`loadingV2Models`）：语义上想避免请求中途判定，但判定本身就发生在成功回调里，而该标记要到 `finally` 才复位，回调期间仍为真，把本该成立的判定挡掉。只保留 `hasFetchedV2Models` 一个判据。
- **对所有调用方统一回退**：会让设置页、知识图谱、定时任务、评测弹窗的已失效配置被自动改写，其中多处是管理员显式配置的结果。
- **改 `options.py::resolve` 校验 `default_model`**：能让管理员保存配置时得到反馈，但该函数是纯同步解析，引入模型缓存依赖会把配置层与运行时缓存耦合；且默认值失效属于管理员配置问题，本次在运行时上下文层解决即可。

## 后果

- 模型下架后聊天页自动切到列表首项并给出一次 warning，历史会话可以正常打开并继续对话。
- 设置页行为不变：失效配置仍原样展示。
- 系统默认对话模型自身被下架时，运行时上下文回落到可用列表首项，全站不再因 `default_model` 失效而整体不可用。
- 会话与 Run 中记录的 `model_spec` 保持原值（读取路径不回写），因此模型重新上架后用户切回该模型的行为不受影响。
- 后端 422 语义与 `test_agent_run_service.py` 的既有断言不变。

## 验证

- 前端单测：`docker exec test-web-1 sh -c "cd /app && node --test test/unit/conversationModelBinding.test.js"` → 19 passed / 0 failed。
- 后端单测：`docker exec test-api-1 sh -c "cd /app && python -m pytest test/unit/agents/test_context_model_fallback.py -q"` → 10 passed / 0 failed。
- 真实浏览器端到端（Playwright + 真实 dev server + 真实后端，本地实际下架 `qwen3.7-max` 后验证）：打开绑定该模型的会话 `999ceaa2-7da8-417c-aed3-3246f25ad82f` → 输入框模型名由 `qwen3.7-max` 自动切为 `qwen3.7-plus`，控制台输出 `[模型选择] 模型 alibaba-cn:qwen3.7-max 已不可用，自动切换到 alibaba-cn:qwen3.7-plus`；同一时刻 `GET /api/chat/thread/{id}/state` 由 500 恢复为 200。
- 跨会话切换端到端：同一页面内先打开会话 A（回退为 plus），再从侧栏切到同样绑定 `qwen3.7-max` 的会话 B → B 也回退为 plus，控制台输出两条回退日志。该场景曾复现失败（A 正常、B 仍显示 `qwen3.7-max`），由负向案例 6/7 对应的两处修复覆盖。
- 负向案例（5 个，各精确失败对应用例后恢复）：
  1. 摘掉 `recoverFromUnavailableModel` 的场景限制 → 「选择器在拿到列表后校验当前模型并在失效时回退」失败。
  2. 失效判定恒 `return false`（等价修复前行为）→ 同一用例失败。
  3. `initializeDefaultModel` 触发条件退回只认 `autoSelectFirst` → 「偏好失效时 autoSelectFirst 为 false 仍能拉到列表并回退」失败。
  4. 失效判定加回「请求进行中」守卫 → 「选择器在拿到列表后校验当前模型并在失效时回退」失败（断言需精确到函数体，用宽松跨段正则会漏）。
  5. `prepare_agent_runtime_context` 退回「只判空」→ 「运行时上下文必须在模型下架时回落到可用模型」失败（只测辅助函数会全绿，必须断言调用点）。
  6. 删掉 `selectionScope` 的 watch → 「切换会话时重新判定失效，不依赖再次拉取列表」失败。
  7. 幂等键退回只按 spec → 「幂等去重按会话范围，跨会话的第二个失效会话仍会回退」失败。
- Lint：`npx eslint src/components/ModelSelectorComponent.vue --max-warnings=0` → 退出码 0。全量前端单测 475/473/2，与改动前基线逐位一致（2 例失败来自工作区其他未合并 PR，与本改动无关）。
- 未验证：worker 侧上下文压缩、子 Agent 运行等异步路径未做浏览器端到端，仅由 `resolve_available_default_chat_model` 的单测覆盖其回落行为。
