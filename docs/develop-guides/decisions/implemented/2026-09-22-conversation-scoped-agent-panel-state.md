# 右侧面板状态按会话隔离

状态：implemented
类型：bug-fix
Owner：web/src/components/AgentChatComponent.vue

## 问题

在会话 A 上传文件并点击「在右侧面板查看」后，切到会话 B，右侧面板仍然打开并展示会话 A 的文件；两个会话各自上传过文件时，切回会话 A 看到的是会话 B 的文件。

根因是「切换会话」这一动作存在两个入口，而面板状态重置只挂在其中一个上面：

- `web/src/components/AgentChatComponent.vue` 的 `selectChat()` 原本只在 `previousThreadId !== chatId` 时调用 `resetAgentPanelState()`。
- 侧边栏会话列表（`web/src/layouts/AppLayout.vue` 的 `handleSelectChat`）点击时先 `chatThreadsStore.setCurrentThreadId(threadId)`，再 `router.push`；`AppLayout` 的 route watcher 也会直接改写同一个 store。等路由变化把 `selectChat(新id)` 调起来时，`currentThreadId` 已经是新会话，`previousThreadId === chatId`，重置分支被跳过。
- 组件级 ref（`isFilePanelOpen`、`agentPanelPreviewTabs`、`agentPanelActivePreviewPath`、`agentPanelSections` 等）跨会话共享，于是原样带进新会话；`AgentPanel` 在 `threadId` 变化时会拿上一个会话的 `activePreviewPath` 去新会话重新取文件（`AgentPanel.vue` 的 `watch([threadId, activePreviewPath], loadActivePreview)`），失败时兜底成 403/失败态，表现为「文件查看状态被另一会话接管」。

预览内容缓存本身按 `${threadId}:${path}` 分键，没有串号；串的是面板状态。

恢复机制上线后立刻暴露第二个问题：切回有预览的会话时，面板会「先显示内容、再闪一下重新加载」。实测切回瞬间对同一 artifact 发出 **2 次**预览请求：第一发来自面板状态恢复（`activePreviewPath` 变化触发 `loadActivePreview`），第二发来自 `selectChat` 末尾的 `handleAgentStateRefresh(chatId)` —— 它递增 `agentPanelFilesystemRefreshVersion`，而 `AgentPanel` 对该版本号的响应是「作废当前预览缓存 + 重新加载」。而在切换会话时，第一发拉到的已经是最新内容，第二发纯属重复。

## 决策

（一）面板状态的语义 Owner 收敛到 `watch(currentChatId)`

把面板状态的语义 Owner 收敛到 `AgentChatComponent` 的 `watch(currentChatId)`：无论 store 被谁改写，切换都由该 watcher 统一处理。

- 新增 `threadPanelStateMap`（`threadId -> 面板状态快照`），快照覆盖 `isFilePanelOpen`、`statePanelOpen`、`isAgentPanelMaximized`、`agentPanelPreviewTabs`、`agentPanelActivePreviewPath`、`agentPanelViewMode`、`agentPanelSections`、`agentPanelActiveSectionKey`。
- watcher 中先 `saveAgentPanelStateForThread(oldThreadId)` 再 `restoreAgentPanelStateForThread(threadId)`；目标会话没有快照（首次进入该会话、新建对话、会话 id 为 `null`）时回到默认关闭态。
- 快照只在「面板打开或存在预览标签」时保留，其余情况删除条目，避免访问过的会话各留一份快照导致 Map 无限增长。
- 恢复时清空 `filePanelDragWidth`，让恢复后的 `panelRatio` 生效（`filePanelWidthStyle` 优先使用拖拽宽度）。
- 恢复状态时同步恢复 `statePanelOpen`（状态面板与文件面板同属右侧面板），避免两个面板的开关状态在会话间错位。
- 删除 `selectChat`、`selectThreadFromRoute`、`loadChatsList`、`currentAgentId` watcher 中四处显式的 `resetAgentPanelState()`：它们都在 `setCurrentThreadId` 之前同步执行，会先清空面板状态，使随后的存档拿到的是「已被清空」的状态，会话记忆失效。

`sections` / `previewTabs` 存快照前做浅拷贝：虽然当前所有更新路径都走不可变更新（`web/src/utils/agentPanelSections.js` 返回新数组），但不把「后续不会就地修改」当作机制保证。

（二）切换会话时的状态刷新不再递增文件系统刷新版本号

`handleAgentStateRefresh(threadId, { bumpFilesystemRefresh = true })` 新增开关，`selectChat` 传 `bumpFilesystemRefresh: isSameThreadReselect`。

- 版本号的语义是「文件系统内容可能变了，重新读一遍」，因此手动刷新按钮、面板刷新按钮、流式期间的状态刷新都保持递增。
- 进入会话时目录树与预览都刚按新 `threadId` 重新读取过——目录树由 `AgentPanel` 的 `threadId` watcher 在 `filesystemVisible` 时 `refreshFileSystem({ ensure: true })` 负责，预览由 `loadActivePreview` 负责；因此递增版本号在切换场景下没有任何新信息，只带来一次作废重载。
- `isSameThreadReselect = previousThreadId === chatId && lastLoadedThreadId === chatId`：只有「重复选中当前已加载的会话」（例如从其它页面点回同一会话）才保留既有刷新语义。不能简单写成 `chatId !== previousThreadId`——侧边栏/路由会先改写 store，`previousThreadId` 在切换场景下也已经等于目标会话，该表达式恒为「未切换」，会把重复选中一并误伤成不刷新。组件新增 `lastLoadedThreadId`（最近一次完成加载的会话）来区分这两种情况。
- 未改 `AgentPanel`：该组件对版本号的响应（作废缓存 + 重载）对同会话内的文件变化是正确行为，问题出在「不该发这个信号」，而不是「收到信号后处理错了」。
- 对真正切换的场景，跳过 bump 可证明无损：受该开关影响的只有「非 Workdir、非 workspace」的 artifact 预览（`AgentPanel` 的版本号 watcher 本就排除 `workdir` / `workspace` 标签），而 `AgentPanel` 的 `threadId` watcher 在进入会话时已按 `${threadId}:` 前缀清掉该会话的预览缓存，紧接着 `loadActivePreview` 必然重新拉取；因此不存在「跳过刷新后看到旧内容」的场景。

## 替代方案

- 切换会话一律关闭右侧面板（不保留每会话记忆）：改动更小，但切回原会话要重新点开文件，与「每个会话记住自己看过的文件」的预期不符。
- 只修侧边栏：让 `handleSelectChat` 不再直接改写 store，交由 `selectChat` 统一处理。这会削弱侧边栏选中态的即时反馈，且 `AppLayout` 的 route watcher 仍会直接改写 store，缺陷会从另一条路径复现。
- 在 `AgentPanel` 内部按 threadId 缓存面板状态：面板组件不拥有「当前是哪个会话」的决策权，`isFilePanelOpen`、`agentPanelViewMode`、`panelRatio` 等状态在父组件，拆分后需要双向同步，反而增加耦合。
- 把 `panelRatio`（面板宽度比例）也做成按会话记忆：宽度属于跨会话的界面偏好，做成按会话会让同一用户的宽度观感在会话间不一致，不采纳。
- 消除重复加载的另一种做法：在 `AgentPanel` 里按时间戳判断「刚加载过就忽略本次版本号变化」。要引入 `threadActivatedAt` / 预览加载完成时刻两个时间量，把「是否重复」判断塞进消费方，且对未来新增的版本号来源不友好；在信号发出侧收敛更直接。
- 让 `selectChat` 完全不调用 `handleAgentStateRefresh`：会一并丢掉 `fetchAgentState` / `fetchThreadAttachments`（状态面板与附件列表需要它们），范围过大。
- `selectChat` 一律传 `bumpFilesystemRefresh: false`：实现更短，但会把「重复选中当前会话」也变成不刷新，丢失既有的「重新读一次」行为（由独立 Reviewer 指出，已改为按 `isSameThreadReselect` 区分）。
- 用 `chatId !== previousThreadId` 判断是否发生切换：侧边栏/路由先改写 store，该条件在真实的切换场景下也成立为「未切换」，无法区分，故不采用。

## 后果

- 切到未打开过面板的会话时面板关闭；切回打开过文件的会话时恢复该会话自己的标签页与当前预览文件。
- 每个真实打开过面板的会话在内存中保留一份快照（标签页、分区与当前路径），面板关闭且无标签的会话不留条目。
- `resetAgentPanelState()` 现在只由「无快照」分支调用；新建对话与切到无历史的面板状态等价于旧行为。
- 已删除的会话会留下一条无引用的快照条目（会话 id 为 uuid，不会被复用），量级与用户打开过面板的会话数相同，接受。
- 会话记忆随组件实例存活：页面刷新、或组件被真正卸载（`<keep-alive>` 缓存被回收）时全部清空，退化为「每次切换都重置」。不做持久化。
- 切回会话时面板加载一次即稳定（`closed → loading-preview → ready`），不再出现「内容已显示又重新加载」的闪动。
- 「重复选中当前已加载会话」的刷新语义与改造前一致（仍会重新读取一次），因此该路径的行为未变。
- 切换会话后若活动分区是文件预览（非 Workdir 文件），目录树仍按既有惰性策略在用户切到文件分区时加载，与手动打开预览面板的行为一致。
- 未覆盖：`AgentPanel` 内部的目录树展开态、选中态仍按 `threadId` 变化重置（既有行为，本次不扩大范围）。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 切到另一个会话后右侧面板不再残留上一个会话的文件 | 面板跨会话残留，显示别人会话的文件 | `web/src/components/AgentChatComponent.vue` | 真实页面 E2E：会话「文件内容分析」点附件开面板 → 切到「文件分析」→ `#agent-file-panel.is-visible` 为 false，面板文本不含 A 的文件名 | 注释掉 `saveAgentPanelStateForThread` / `restoreAgentPanelStateForThread` 后重跑：`切到会话B 后面板已关闭` FAIL（panelOpen=true，面板文本为 A 的文件路径） | Passed |
| 两个各自上传过文件的会话互不串号 | 切回 A 看到 B 的文件 | 同上 | 同上 E2E：在「文件分析」点自己的附件（`2026年下半年黄金趋势分析.md`）→ 切回「文件内容分析」→ 面板活动文件仍为 `ScreenShot_2025-12-03_165001_489.png`，不含 B 的文件名 | 同上负向实验：`会话A 不显示会话B的文件` FAIL（面板文本同时出现 B 的文件名与路径） | Passed |
| 切到未打开过面板的会话时面板关闭 | 面板在无状态会话中继续打开 | 同上 | 同上 E2E：切到「日常问候」（无附件、另一个智能体）→ 面板关闭 | 同上负向实验：`切到无附件会话C 后面板关闭` FAIL | Passed |
| 存档/恢复的装配链与「不得提前重置」被固定 | 后续改动把重置挪回 `selectChat` 导致记忆失效 | 同上 + `web/test/unit/agentPanelPerThreadState.test.js` | 容器内相关子集：`node --test test/unit/agentPanelPerThreadState.test.js …` → 新增 4 例全通过（快照字段、存档先于恢复、`selectChat` 不含 `resetAgentPanelState()`、状态刷新不带 `bumpFilesystemRefresh`） | 把 `resetAgentPanelState()` 写回 `selectChat` 时第三条用例因 `doesNotMatch` 失败 | Passed |
| 切回会话时同一份预览只加载一次（不再闪动） | 内容先显示、随后被作废重载 | `web/src/components/AgentChatComponent.vue` | 探针脚本 `.workbuddy/tmp/panel_flicker_probe.cjs`：切回瞬间对同一 artifact 的预览请求由 **2 条降为 1 条**；预览区状态时间线由 `closed → ready → ready(二次渲染)` 变为 `closed → loading-preview(73ms) → ready(180ms)` | 把该出参改回默认（即不传开关）时同一窗口内预览请求回到 2 条 | Passed |
| 重复选中当前会话仍保留刷新语义 | 同会话重进不再重新读取预览/目录树 | 同上 | 代码核对：`isSameThreadReselect` 走 `bumpFilesystemRefresh: true`，与改造前一致；该路径不改动面板状态，因此不产生闪动 | — | Inspected（未做交互验证） |
| 快照不依赖"后续只做不可变更新"的约定 | 就地改动 section/tab 数组污染已存档快照 | 同上 | `snapshotAgentPanelState` 对 `previewTabs` / `sections` 做浅拷贝，单测断言锁定该形式 | — | Passed |
| 改动不破坏既有前端链路 | lint 或构建失败 | `web/src/components/AgentChatComponent.vue` | `eslint --max-warnings=0` 通过；`pnpm run build` → `✓ built in 4.42s` | — | Passed |

执行命令与结果：

- E2E（真实 dev server + 真实令牌，全部走侧边栏点击切会话）：
  `NODE_PATH=<node workspace>/node_modules node .workbuddy/tmp/panel_thread_scope_e2e.cjs` → **PASS 9 / FAIL 0**。
- 负向实验（临时停用 `saveAgentPanelStateForThread` / `restoreAgentPanelStateForThread`，其余不变）：**PASS 5 / FAIL 4**，失败项与缺陷一一对应，确认 E2E 是可失败的 oracle。（只在 `selectChat` 里加回 `resetAgentPanelState()` 不会复现：该分支在侧边栏路径下本就被跳过。）
- 闪动探针（`.workbuddy/tmp/panel_flicker_probe.cjs`，记录切回瞬间的 API 请求与预览区状态时间线）：
  - 仅面板状态隔离修复后：同一 artifact 预览请求 **2 条**，时间线 `closed → ready(80ms) → ready 二次渲染(241ms)`。
  - 追加本决策第（二）项后：预览请求 **1 条**，时间线 `closed → loading-preview(73ms) → ready(180ms)`。
- 相关单测子集（容器内）：
  `docker exec test-web-1 sh -c "cd /app && node --test --test-concurrency=1 test/unit/agentPanelPerThreadState.test.js test/unit/agentPanelSections.test.js test/unit/agentPanelFilesystemPolling.test.js test/unit/messageAttachmentPreview.test.js"`
  → **27 tests / 26 passed / 1 failed**；唯一失败为既有失败 `agentPanelSections.test.js::Run 刷新会丢弃同路径 artifact 的旧预览并重新读取`（它在 `AgentPanel.vue` 中查找 `watch(\n  () => props.activePreviewPath` 标记，该标记已不存在；本次未改动 `AgentPanel.vue`）。
- lint：`docker exec test-web-1 sh -c "cd /app && pnpm exec eslint src/components/AgentChatComponent.vue test/unit/agentPanelPerThreadState.test.js --max-warnings=0"` → 通过。
- 构建（含本决策全部改动）：`docker exec test-web-1 sh -c "cd /app && pnpm run build"` → `✓ 7340 modules transformed / ✓ built in 4.42s`。
- 全量前端单测在容器内耗时超过 22 分钟未跑完（`test/**` 中部分用例各自耗时 20~90 秒，且 vite 反复 re-optimize），已中止；中止前 **77 passed / 2 failed**，两个失败均为既有失败（上述 `agentPanelSections` 标记用例、`登录 423 保留锁定状态…`）。宿主无法执行前端单测：`web/node_modules` 缺失，未安装依赖的用例直接 `ERR_MODULE_NOT_FOUND: vite`。
- 未执行项：多标签页并发切换会话的交互验证（同一浏览器两个标签页）；Workdir 文件预览在切回会话时的重复加载未单独测量（该路径由 `refreshActivePreviewIfChanged` 的「元数据变化才重载」条件控制，与本次开关无关）；「重复选中当前会话」的刷新行为只做了代码核对，未做交互验证。
- 证据缺口：上述 E2E 与探针脚本位于被 Git 忽略的 `.workbuddy/tmp/`，不在仓库内，CI 无法复跑；仓库内的自动化护栏是 `web/test/unit/agentPanelPerThreadState.test.js` 的源码装配断言（不覆盖运行时隔离与闪动）。若后续需要把这两条行为纳入 CI，需要先引入浏览器级 E2E 基座，属独立事项。

## 独立语义 Review

由不继承开发上下文的独立 Reviewer Agent 完成（2026-09-22），覆盖本决策的完整 diff、新增单测与决策记录，并自行核对了 `AgentChatComponent.vue`、`AgentPanel.vue`、`AppLayout.vue`、`views/AgentView.vue`、`stores/chatThreads.js` 的实际调用链：

- 阻断问题：无。
- 重要（已处置）：① `selectChat` 无条件传 `bumpFilesystemRefresh: false` 会误伤「重复选中当前会话」的既有刷新语义 → 改为按 `isSameThreadReselect` 区分；② 快照直接存数组引用，依赖「后续只做不可变更新」的约定 → 改为浅拷贝并用单测锁定。
- 次要：① `threadPanelStateMap` 随组件实例存活这一限制未写明 → 已在代码注释与「后果」中补充；② 决策记录中时间线措辞与探针输出不一致 → 已统一为 `closed → loading-preview → ready`。
- 未发现问题：删除四处显式重置后不存在「该关未关」的路径（逐一核对 `selectChat` / `selectThreadFromRoute(null)` / `loadChatsList` / `currentAgentId` watcher，只有 `null→null`、`A→A` 不触发 watcher，此时不存在需清理的上一个会话状态）；`AgentPanel` 三个 watcher 与父组件新状态自洽（缓存键一致、对象 URL 释放与失效顺序正确）；其余 `handleAgentStateRefresh` 调用方保持 `true` 合理，未发现依赖「切换后必定刷新」的隐藏 consumer；`selectChat` 失败回滚路径下面板状态仍正确。
- 证据缺口（Reviewer 指出，本次不处置）：真实 E2E 与探针脚本未入库，合并后 CI 无法复跑这两条行为。

## 关系

复用 `web/src/utils/agentPanelSections.js` 的不可变分区更新语义，未改变其接口。
