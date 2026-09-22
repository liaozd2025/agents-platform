# 新建会话沿用用户上次手动选择的模型

状态：implemented
类型：feature
Owner：web/src/utils/conversationModel.js

## 问题

聊天页的模型只有会话维度的状态：组件内的 `selectedModelByThread` 和会话自身绑定的 `thread.metadata.model_spec`。新建会话时两者都为空，`resolveConversationModel` 返回空值，模型选择器随即按 `auto-select-first` 兜底选中「已配置模型列表首项」。结果是：用户在会话里手动选了 `qwen3.7-plus`，点「新建对话」后选择器又回到 `qwen3.7-max`，用户的选择在每次新建会话时都被丢弃。

用户级偏好在这一版实现里没有位置：`d56c3a54` 把原先硬编码的系统默认模型改成「留空 + 选择器兜底首项」，兜底值来自后端列表顺序，用户的手动选择因此成为会话内的临时状态。

## 决策

引入用户级本地偏好 `yuxi_chat_model`（`localStorage`），把模型解析链扩展为四级：

```
本会话显式选择 → 会话绑定 model_spec → 用户本地偏好 → 空值（由选择器兜底首项）
```

- `web/src/utils/conversationModel.js` 持有偏好读写与解析：新增 `readChatModelPreference` / `writeChatModelPreference` / `clearChatModelPreference`（可注入存储实现，读写失败不抛异常），`resolveConversationModel` 增加 `savedModel` 入参，排在会话绑定之后。
- `web/src/components/AgentChatComponent.vue` 负责写入时机：`handleModelSelect` 在**用户手动选择**时写入偏好、清空时清除偏好；`savedChatModel` 在组件初始化时读取，使刷新页面后仍生效。
- `web/src/components/ModelSelectorComponent.vue` 的兜底选择带 `autoSelected: true` 标记随事件上报。调用方据此区分「用户选择」与「系统兜底」，兜底首项不写偏好，保留「用户从未选择过」的语义——这样后端调整模型列表顺序后，未手动选择过的用户仍跟随新的首项。

已有会话继续以自身 `model_spec` 为准：偏好只在会话维度缺失时生效，切回旧会话不会被偏好改写。

## 替代方案

- 只做内存记忆（新建会话时把当前 `model_spec` 复制进草稿槽位）：改动更小，但刷新页面即失效，新建会话仍会回到首项，属于半成品交互。
- 新会话继承「上一个会话」的模型：结果依赖用户切换会话的顺序，语义不稳定，且覆盖不到尚无任何会话的场景。
- 恢复硬编码系统默认模型：系统默认模型未配置时选择器会显示不可用模型，已在前一版被否决。
- 偏好按智能体维度存储：多一层状态与旧数据迁移，而用户诉求是「新建会话别把选择重置」，未采纳。

## 后果

- 手动选择过模型的用户，新建会话直接沿用该模型；从未手动选择的用户行为不变（仍取列表首项）。
- 偏好指向的模型被下线时不再回落到首项，而是显示该模型并由后端在发送时拒绝，与会话绑定 `model_spec` 失效是同一种风险；用户改选一次即可恢复。
- 新建会话的 `currentModelSpec` 提前非空，发送按钮不再等待模型列表加载完成才可用。
- 偏好是用户级而非工作区级，同一浏览器内多个标签页共用同一偏好。

## 验证

- 单测：`node --test test/unit/conversationModelBinding.test.js` → 8 passed / 0 failed。新增覆盖偏好优先级（会话绑定优先于偏好、偏好补位新会话）、存储桩读写与清除、存储缺失时不抛异常。
- 负向案例：把 `ModelSelectorComponent` 兜底事件的 `{ autoSelected: true }` 标记去掉后重跑端到端脚本 → 「兜底首项不写入偏好」失败（`yuxi_chat_model="alibaba-cn:qwen3.7-max"`），恢复后 6 passed / 0 failed。即兜底写入偏好正是该 guard 防住的缺陷。
- 端到端（真实 dev server + Playwright + 真实后端模型列表，`.workbuddy/tmp/model_pref_e2e.cjs`）：首次进入显示兜底首项 `qwen3.7-max` 且偏好为空 → 手动选 `qwen3.7-plus` 写入偏好 → 打开已有会话显示其自身绑定的 `qwen3.7-flash`（会话绑定优先未回归）→ 侧边栏「新建对话」后显示 `qwen3.7-plus`，未回到首项。6 passed / 0 failed。
- 静态检查：`pnpm exec eslint` 检查两个组件、工具模块与测试文件 → exit 0。
- 构建：容器内 `pnpm run build` → 成功（4.61s）。
- 全量单测：容器内 `pnpm run test:unit` → 仅 2 例失败，均与本变更无关：`agentPanelSections` 的「Run 刷新会丢弃同路径 artifact 的旧预览并重新读取」（既有本机固定失败），以及 `layout_startup` 的「布局导航不等待品牌或知识库」——失败点是 `useOAEmbedBridge.startBridge` 读取测试桩中未定义的 `window.location.pathname`，涉及的文件不在本变更范围内（其改动来自工作区未提交的 OA 嵌入修复）。
- 未运行：其他浏览器与多标签页并发切换模型的场景。
