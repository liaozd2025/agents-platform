# 历史会话切换用骨架屏替代初始空态

状态：implemented
类型：bug-fix
Owner：web/src/components/AgentChatComponent.vue

## 问题

切换历史会话时页面先渲染一次初始会话页（embed 模式下的 logo 与欢迎语、输入框居中态、独立站的随机欢迎语），随后才显示完整对话历史。

根因是两段状态在时间上不同步。`currentThreadId` 在切换动作一开始就被改写（`AppLayout.handleSelectChat`、`AppLayout` 的路由 watch、`selectChat` 三处），而消息要等 `fetchAgentHistory` 返回后才写入组件内的 `threadMessages`。这段空窗里 `conversations` 由空数组派生，模板中三处基于 `!conversations.length` 的空态判定全部命中：

- embed 欢迎区（logo、产品名、说明文案）；
- 底部输入区的 `start-screen` 类，把输入框从底部瞬移到垂直居中；
- 独立站输入框上方的随机欢迎语。

同时 `isLoadingMessages` 在切换之后才置位，且只在输入区上方渲染一个绝对定位的小 spinner，视觉上被欢迎区盖过，所以"加载中"从未被用户感知，"空态"却先出现。

## 决策

以组件内 `threadMessages` 是否存在目标会话的键作为"是否首次加载"的判据，派生 computed `isThreadSkeletonActive`。该 computed 只依赖 `currentChatId` 与 `threadMessages`，在 `currentThreadId` 被改写的同一轮响应式更新内即为真，早于本次渲染，因此骨架屏不会晚于空态出现，也不需要等待 `isLoadingMessages`。

骨架屏渲染在 `.chat-box` 内，占据消息气泡的位置；切换期间不再应用输入区的 `start-screen` 居中样式，输入框保持底部固定且可用；原 `.chat-loading` 小 spinner 在骨架期隐藏，避免两个加载指示叠加。

已加载过的会话（含空数组表示的真实空会话）直接渲染已有内容，不显示骨架。新建对话时 `createThread` 已写入空数组，因此仍显示欢迎态。

骨架的撤下时机由 `threadMessages[threadId]` 的写入决定：请求正常返回时写入历史，请求失败且该会话从未加载过时写入空数组占位；两种情况都会让本次渲染直接出内容或空态，不会停留在骨架。

## 替代方案

- 等数据返回后再改写 `currentThreadId`（预加载）。无闪烁更彻底，但会延迟侧边栏高亮与路由跳转，且需要迁移流式订阅、输入草稿与右侧面板状态的会话级存档时序，改动面与风险远超本次交互问题。
- 直接用 `isLoadingMessages` 作为骨架条件。该标记在侧边栏路径下晚于 `currentThreadId` 改写，中间可能跨多个 macrotask（路由跳转、`selectAgent`），仍会先渲染空态。

## 后果

- 加载失败必须撤下骨架屏，否则 `isThreadSkeletonActive` 会一直为真、骨架无限停留。但 `fetchThreadMessages` 不只在切换会话时被调用（run 终态收敛 `settleThreadAfterTerminal`、中断恢复 `restoreInterruptedRun`、审批提交后的刷新都会走它），无条件写入空数组会把已加载会话正在显示的历史清空，因此只在"从未加载过"（`threadMessages` 无此键）时才写空数组占位。失败后该会话退化为空态并弹出原有错误提示；已加载会话刷新失败则完整保留原内容。
- 清空 `threadMessages` 的路径只有 `watch(currentAgentId)` 内的 `props.singleMode` 分支：只有该分支会**同时** `setCurrentThreadId(null)` **且清空** `threadMessages`（`loadChatsList`、`selectThreadFromRoute` 也会置 null，但不写消息）。此刻 `currentChatId` 为 null、骨架条件为假，显示初始欢迎态是正确语义；随后 `loadChatsList` 会自动进入首个会话，仍走骨架路径。非 `singleMode` 下切换 agent 时该 watch 直接返回，不会重置消息，也就不存在残留骨架。
- 已知取舍：首次加载失败的会话会被写入 `[]` 占位，因此"失败后再次选中该会话"不会再显示骨架，重试窗口内仍会闪一次空态——占位空数组无法区分"失败后的空"与"真正的空会话"。若后续要消除这最后一处闪烁，应改为独立的失败标记（如 `failedThreadIds`），而不是复用空数组。
- 改动集中在 `AgentChatComponent.vue` 的模板、computed 与样式，不新增 store 字段、抽象层或兼容分支。
- 骨架的样式只用 flex 布局、线性渐变与 background-position 动画；`flex gap` 沿用项目既有约定（全局已有 405 处），基线内核对 `gap < 84` 不生效，退化表现只是灰条间距消失。

## 验证

| 主张 | 语义 Owner | 直接证据 | 负向案例 | 当前结果 |
|---|---|---|---|---|
| 切换到未加载的历史会话时首帧为骨架屏，不出现欢迎语、输入框居中态与 spinner | `web/src/components/AgentChatComponent.vue` | 拦截 `GET /api/chat/thread/{id}/history` 延迟 1.2s 放大空窗，20ms 轮询记录 DOM 状态：`convs=5 → 25ms SKELETON → 1365ms convs=3`，初始空态出现 0 次 | 把 `isThreadSkeletonActive` 临时改为恒 `false` 后同一脚本记录到 `26ms GREETING｜START-SCREEN｜SPINNER`，初始空态出现 1 次 | 通过 |
| 历史加载失败时骨架撤下而非常驻，且已加载会话的历史不被清空 | 同上 | 拦截 history 返回 500（延迟 800ms 模拟慢失败）：切到未加载会话的时间线为 `21ms convs=5 → 43ms SKELETON｜convs=0 → 861ms GREETING｜START-SCREEN｜convs=0`，骨架撤下、错误 toast 出现、不常驻；对已加载会话重复选中触发刷新失败后 `convs` 仍为 5（历史完整保留） | 若按"无条件写空数组"实现，阶段 2 的刷新失败会把已加载会话清成 0 条，该断言即为反例 | 通过 |
| 已加载过的会话切回直接渲染内容；新建对话仍显示欢迎态 | 同上 | 新建对话实测状态为 `GREETING｜START-SCREEN｜convs=0`（无骨架） | 同上负向案例覆盖 | 通过 |
| 骨架屏在浅色、深色与窄视口下均可读，且不影响输入框定位 | 同上 | `.workbuddy/tmp/skeleton_light.png`、`skeleton_dark.png`、`skeleton_narrow.png` 三张实测截图 | 无 | 通过 |
| embed（OA iframe 嵌入）下切换历史会话同样先显示骨架，不闪 logo 欢迎区；无会话初始态仍显示欢迎区 | 同上 | 用 route 把宿主 origin `http://192.168.168.172:8889` 替换为只含 iframe 的假 OA 页面并按正式协议回 `login-params` 完成换票，iframe 内切到全屏模式后从侧栏切换会话：时间线 `0ms convs=2 → 33ms SKELETON｜convs=0 → 1688ms convs=1`，切换期间欢迎区出现 0 次；初始态实测为 `EMBED-WELCOME｜START-SCREEN｜convs=0`（截图 `embed_idle.png`、`embed_switch_skeleton.png`） | 同独立站负向案例（同一 `isThreadSkeletonActive` 条件） | 通过 |
| 编译与静态检查 | `web/package.json` | 容器内对提交版本执行 `npx eslint --stdin --stdin-filename src/components/AgentChatComponent.vue --max-warnings=0`（无输出）、`pnpm run lint:check`（全量 eslint `--max-warnings=0`，无输出）；把提交版本放回基线并补齐基线依赖后 `pnpm run build` 成功（`✓ built in 7.70s`） | 无 | 通过 |
| 全量单测 | `web/test/unit/` | `pnpm run test:unit` 未通过：失败用例 `test/unit/layout_startup.test.js`（调用栈指向 `useOAEmbedBridge.js` 读取 `window.location.pathname`）。失败文件均不在本 PR 差异内，且 `useOAEmbedBridge.js` 在工作区带有其他未提交改动 | 无 | 未通过（归因为基线/并发变更，非本 PR 差异） |

验证 embed 分支需要绕过两项与环境相关的限制，均不影响真实 OA 页面：新版内核的 Local Network Access 检查会拦掉「伪造宿主页面 → 内网 IP iframe」（`ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS`，需以 `--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessChecks,BlockInsecurePrivateNetworkRequests` 启动）；fixed 显示模式下历史入口按钮为 `display:none`，会话切换需先切到全屏模式再走主导航侧栏。仍未覆盖：floating 模式下经历史抽屉切换会话（入口不同，骨架分支与前两者共用同一条件）。
