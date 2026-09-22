import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'

// 右侧面板状态必须按会话隔离：切到另一个会话时不能残留上一个会话打开的预览文件。
// 回归背景：侧边栏点击会先直接改写 store 的 currentThreadId（AppLayout.handleSelectChat），
// 于是 selectChat 里 `previousThreadId !== chatId` 的判断失效，面板状态（开关/标签页/当前文件）跨会话残留。
// 这里用源码断言固定"存档 + 恢复"的装配链，并给出负向断言：selectChat 不允许再提前重置面板状态。
const readSource = (relativePath) =>
  readFileSync(new URL(`../../src/${relativePath}`, import.meta.url), 'utf8')

const chatSource = readSource('components/AgentChatComponent.vue')

const sliceBetween = (source, startMarker, endMarker) => {
  const start = source.indexOf(startMarker)
  const end = source.indexOf(endMarker, start)
  assert.notEqual(start, -1, `未找到片段起点：${startMarker}`)
  assert.notEqual(end, -1, `未找到片段终点：${endMarker}`)
  return source.slice(start, end)
}

// 去掉行注释后再做负向断言：注释里出现函数名不应被误判为真实调用。
const stripLineComments = (source) => source.replace(/^\s*\/\/.*$/gm, '')

test('面板状态按 threadId 存档与恢复', () => {
  assert.match(chatSource, /const threadPanelStateMap = new Map\(\)/)
  // 快照必须覆盖决定"看到哪个文件"的全部字段
  const snapshot = sliceBetween(
    chatSource,
    'const snapshotAgentPanelState = ()',
    'const saveAgentPanelStateForThread'
  )
  for (const field of [
    'isFilePanelOpen',
    'statePanelOpen',
    'isAgentPanelMaximized',
    'previewTabs',
    'activePreviewPath',
    'viewMode',
    'sections',
    'activeSectionKey'
  ]) {
    assert.match(snapshot, new RegExp(`${field}: `), `快照缺少字段：${field}`)
  }

  // 恢复时没有快照代表首次进入该会话，必须回到默认关闭态
  const restore = sliceBetween(
    chatSource,
    'const restoreAgentPanelStateForThread = (threadId)',
    'const previewCacheKey'
  )
  assert.match(restore, /if \(!snapshot\) \{\s*resetAgentPanelState\(\)\s*return\s*\}/)
  assert.match(restore, /agentPanelActivePreviewPath\.value = snapshot\.activePreviewPath/)

  // 数组字段必须浅拷贝存档，避免依赖"后续只做不可变更新"这一约定
  assert.match(snapshot, /previewTabs: \[\.\.\.agentPanelPreviewTabs\.value\]/)
  assert.match(snapshot, /sections: \[\.\.\.agentPanelSections\.value\]/)
})

test('切换会话时先存档旧会话再恢复新会话', () => {
  const watcher = sliceBetween(
    chatSource,
    'watch(currentChatId, (threadId, oldThreadId) => {',
    "emit('thread-change'"
  )
  const saveIndex = watcher.indexOf('saveAgentPanelStateForThread(oldThreadId)')
  const restoreIndex = watcher.indexOf('restoreAgentPanelStateForThread(threadId)')

  assert.notEqual(saveIndex, -1, 'watch(currentChatId) 未存档旧会话面板状态')
  assert.notEqual(restoreIndex, -1, 'watch(currentChatId) 未恢复目标会话面板状态')
  // 顺序必须是先存档后恢复，否则会先被新会话状态覆盖
  assert.ok(saveIndex < restoreIndex, '必须先存档旧会话，再恢复目标会话')
})

test('负向：selectChat 不得提前重置面板状态', () => {
  const selectChatBody = stripLineComments(
    sliceBetween(
      chatSource,
      'const selectChat = async (chatId) => {',
      'const selectThreadFromRoute'
    )
  )
  // 提前重置会清空旧会话快照，切回时无法恢复自己的文件
  assert.doesNotMatch(
    selectChatBody,
    /resetAgentPanelState\(\)/,
    'selectChat 内不得调用 resetAgentPanelState()，面板状态由 watch(currentChatId) 统一负责'
  )
})

test('切换会话的状态刷新不重复触发预览重载', () => {
  // 回归背景：切回会话时面板刚按新 threadId 拉取过预览，若此时再递增文件系统版本号，
  // AgentPanel 会作废缓存并重复加载同一份内容，表现为"先显示内容、再闪一下"。
  const refreshBody = stripLineComments(
    sliceBetween(
      chatSource,
      'const handleAgentStateRefresh = async (',
      'const toggleStatePanel'
    )
  )
  assert.match(refreshBody, /bumpFilesystemRefresh = true/)
  assert.match(
    refreshBody,
    /if \(bumpFilesystemRefresh && chatId === currentChatId\.value\) \{/,
    '文件系统版本号递增必须受 bumpFilesystemRefresh 开关约束'
  )

  const selectChatBody = stripLineComments(
    sliceBetween(
      chatSource,
      'const selectChat = async (chatId) => {',
      'const selectThreadFromRoute'
    )
  )
  // 侧边栏会先改写 store，previousThreadId 已等于目标会话，必须用 lastLoadedThreadId 才能区分
  // "真的切了会话"与"重复选中当前会话"；重复选中要保留原有的刷新语义。
  assert.match(
    selectChatBody,
    /const isSameThreadReselect = previousThreadId === chatId && lastLoadedThreadId === chatId/
  )
  assert.match(
    selectChatBody,
    /handleAgentStateRefresh\(chatId, \{ bumpFilesystemRefresh: isSameThreadReselect \}\)/,
    '只有真的切换会话时才跳过文件系统版本号递增'
  )
  assert.match(selectChatBody, /lastLoadedThreadId = chatId/)
})
