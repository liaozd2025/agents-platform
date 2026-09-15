# Run 终态收敛的单一出口

状态：implemented
类型：bug-fix
Owner：web/src/composables/useAgentRunStream.js

## 问题

Run 的终态可能先被「页面重新可见时恢复」的 resume 分支发现，也可能先收到 Run SSE 的 finished 事件。resume 分支在判定 run 已终态后清空 `activeRunId`，但不刷新线程历史、不清本轮实时消息；随后到达的 finished 会让 `finalizeRunStream` 的 `activeRunId !== runId` 早退判断直接返回，收尾整段被跳过。本轮实时消息因此残留在 `onGoingConv` 中，被渲染成一条 `status` 为 streaming 的尾巴轮：该轮自身不渲染 refs，其前一轮又被 `isConversationSettled` 判为「未收尾」，两轮的末轮 refs 都不渲染。表现为 iframe 内嵌对话中回复完成后「来源」按钮不出现、刷新页面后按历史分组重新渲染才恢复；独立站很少在收尾窗口内发生 visibility 变化，因此不复现。

## 决策

终态收敛只有一个出口：`settleThreadAfterTerminal(threadId, runId, { delay })` 负责刷新线程历史，并在没有新 run 接管时清掉本轮实时消息。`finalizeRunStream` 与 `resumeActiveRunForThread` 的终态分支都调用它；末尾兜底分支只在「服务端权威确认没有活跃 run」且「存在残留实时消息」（`hasOngoingRunChunks`）时调用——查询活跃 run 失败时不清场，避免丢掉尚未落库的实时输出，也避免每次页面可见都产生一次历史请求。

## 替代方案

1. 只在渲染侧放宽：让 streaming 尾巴轮也渲染 refs，或让 `isConversationSettled` 忽略尾部实时轮。这会把「仍在生成」的真实形态显示为已收尾，掩盖 Run 未收敛的事实，违反「不用乐观 UI 覆盖数据库最终事实」的约束。
2. 在 `finalizeRunStream` 的早退分支里补收敛。早退发生时触发方是 resume 分支，已不在该调用栈上下文中；两处都要判断残留，且覆盖不到「SSE 完全没到达」的情形。
3. 禁止 resume 分支清空 `activeRunId`。该清空是「本地流还开着但 run 已终态」的正确状态交接，去掉会让前端长期持有已结束的 run。

## 后果

- 每次终态收敛最多多一次线程历史 GET（幂等只读）；清理实时消息带「无新 run 接管」保护，不会误清下一轮正在流式的输出。
- 兜底收敛还需「服务端确认无活跃 run」，查询异常时保持现状；该收敛不会在 run 可能仍在跑时清场。
- 收敛动作统一后，末轮 refs（来源、耗时、复制）与「生成中」指示器的结束时机都跟随同一份历史事实。
- 收敛延迟仍为既有的 200ms，用于等待后端事务提交；不新增配置项或降级路径。

## 验证

新增 3 条回归用例（`web/test/unit/agentRequestQueue.test.js`），分别锁住两处新守卫与新增的清场保护：

1. 「本地流还开着但 Run 已终态时收敛视图，避免残留实时消息挡住末轮来源」——resume 终态分支。
2. 「无活跃 Run 且仍有实时消息残留时补一次收敛」——末尾兜底分支。
3. 「活跃 Run 查询失败时不清实时消息」——兜底分支的权威确认保护。

- 正向：容器内 `docker exec yuxi-web-1 sh -c "cd /app && node --test test/unit/agentRequestQueue.test.js"` → 10/10 pass。前置：compose 只挂载 `web/src`，`web/test` 在容器内是镜像旧副本，跑前需 `docker cp web/test/unit/agentRequestQueue.test.js yuxi-web-1:/app/test/unit/`。
- 负向：逐条独立回退每个守卫后跑同一文件——移除兜底分支的收敛调用 → 仅用例 2 失败（9 pass / 1 fail）；去掉权威确认保护 → 仅用例 3 失败（9 pass / 1 fail）。两者都在预期的原因上变红，恢复后 10/10 pass。
- 静态与编译级：`npx eslint src/composables/useAgentRunStream.js test/unit/agentRequestQueue.test.js --max-warnings=0` 通过，整仓 `npm run lint:check` 通过；容器内 `npx esbuild src/composables/useAgentRunStream.js --bundle --alias:@=./src` 成功。
- 未执行：真实 iframe 宿主页复现、全量 `test:unit` 与 `vite build`（本机无 `web/node_modules`，容器内全量运行受 vite 冷启动耗时限制），交由 CI 的 `Lint, unit and production build` 与真机复测覆盖。
