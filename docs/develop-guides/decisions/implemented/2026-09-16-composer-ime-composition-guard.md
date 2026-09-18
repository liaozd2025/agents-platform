# 输入法组合态下的回车不参与发送

状态：implemented
类型：bug-fix
Owner：web/src/components/MessageInputComponent.vue

## 问题

macOS 上用中文输入法输入英文字母时，字母先进入组合态（预编辑），此时回车的含义是「确认上屏」。输入框的发送判定只看 `e.key === 'Enter'` 与 Shift 修饰键，没有过滤组合态，于是这次回车直接把消息发了出去。Windows 复现不到：微软拼音在组合态下只上报 `key="Process"`、`keyCode 229`，`e.key === 'Enter'` 不成立，缺陷被输入法的上报方式掩盖。同一缺陷也会命中 `@` 提及弹窗的回车导航。

## 决策

在输入框与发送判定两层过滤组合态。

- `web/src/components/MessageInputComponent.vue` 新增 `isImeCompositionKey()`，四类判据任一成立即视为组合态内按键：组件自身维护的 `isComposing`、事件自带的 `e.isComposing`、`keyCode === 229`（部分输入法只上报 Process 键）、以及 `compositionend` 之后 100ms 的时间窗。`handleKeyPress` 开头据此早退，同时覆盖向父组件透传发送判定与提及弹窗回车导航两条路径。
- 时间窗为 Safari 保留：它在 `compositionend` 之后紧接着派发的那次回车事件上 `isComposing` 可能已为 false，只信事件字段会漏判。
- `web/src/components/AgentInputArea.vue` 的 `handleKeyDown` 在回车发送分支之前再加 `e.isComposing || e.keyCode === 229` 兜底，让发送判定自己的 Owner 也闭合该边界。

## 替代方案

- 只改发送判定一处：改动最小，但覆盖不到提及弹窗的回车导航，且发送判定只拿到事件字段，Safari 时序下仍会漏。
- 只在输入框侧过滤：发送判定会依赖上游是否漏传事件，边界没有在计费点上闭合。
- 按 `navigator.platform` 分支处理：平台与输入法实现并不一一对应，Linux 的中文输入法同样会走组合态。
- 改由 keyup 触发发送：改变既有交互语义，还要重新处理长按与重复触发。

## 后果

- 组合态内的按键不再发送消息或确认弹窗，macOS 与 Windows 行为一致。
- 时间窗会吞掉「组合结束到回车之间不足 100ms」的一次发送按键，需要用户在极短时间内完成确认与发送两个动作。
- 组合态内的其他按键（如 Tab、Escape）同样被早退，它们在发送与提及导航中本来就没有语义。

## 验证

- 单测：`node --test test/unit/composerImeCompositionGuard.test.js` → 3 passed / 0 failed。
- 负向案例：把 `MessageInputComponent` 的早退与 `AgentInputArea` 的兜底改成恒假后重跑 → 2 个用例失败（分别断言早退早于透传、兜底早于发送分支），恢复后 3 passed。
- 静态检查：`node_modules/.bin/eslint` 检查两个组件与新增测试 → exit 0。
- 构建：`vite build --outDir <TEMP>` → 成功（8.3s），单文件组件编译通过。
- 全量单测：`node --test "test/**/*.test.js"` → 281 tests，稳定失败仅 `test/unit/agentPanelSections.test.js` 的「Run 刷新会丢弃同路径 artifact 的旧预览并重新读取」，该文件只读取 `AgentPanel.vue`，本变更未修改它；两次运行中出现的文件级失败单独重跑均通过，属本机并发抖动。
- 未运行：真实 macOS 中文输入法的端到端手工验证。本机为 Windows，无可用 macOS 环境，组合态事件未在真实输入法下派发。
