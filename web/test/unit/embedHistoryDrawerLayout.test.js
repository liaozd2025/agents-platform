import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('iframe 历史抽屉把会话列表约束为独立滚动容器', () => {
  const source = readFileSync(new URL('../../src/views/AgentView.vue', import.meta.url), 'utf8')

  assert.match(source, /class="embed-history-drawer"/)
  assert.match(
    source,
    /:global\(\.embed-history-drawer\s+\.ant-drawer-body\)\s*\{[\s\S]*?display:\s*flex;[\s\S]*?min-height:\s*0;/
  )
  assert.match(
    source,
    /:global\(\.embed-history-drawer\s+\.conversation-nav-section\)\s*\{[\s\S]*?height:\s*100%;/
  )
})

test('iframe 全屏初始态隐藏标题并让欢迎区与输入区垂直居中', () => {
  const viewSource = readFileSync(new URL('../../src/views/AgentView.vue', import.meta.url), 'utf8')
  const chatSource = readFileSync(
    new URL('../../src/components/AgentChatComponent.vue', import.meta.url),
    'utf8'
  )

  assert.match(viewSource, /:embed-display-mode="embedDisplayMode"/)
  assert.match(viewSource, /v-if="embedMode && embedDisplayMode !== 'fullscreen'"/)
  assert.match(chatSource, /'is-embed-fullscreen': props\.embedMode && props\.embedDisplayMode === 'fullscreen'/)
  assert.match(chatSource, /\.chat\.is-embed-fullscreen \.embed-welcome\s*\{[\s\S]*?top:\s*calc\(50% - 150px\)/)
  assert.match(chatSource, /\.chat\.is-embed-fullscreen \.bottom\.start-screen\s*\{[\s\S]*?top:\s*calc\(50% \+ 42px\)/)
  assert.match(chatSource, /\.chat\.is-embed-fullscreen \.bottom\.start-screen\s*\{[\s\S]*?left:\s*50%;[\s\S]*?width:\s*min\(800px, calc\(100% - 28px\)\)/)
  assert.match(chatSource, /\.chat\.is-embed-fullscreen \.bottom\.start-screen[\s\S]*?\.message-input-wrapper\s*\{[\s\S]*?max-width:\s*none;/)
  assert.match(chatSource, /\.chat\.is-embedded \.bottom\.start-screen\s*\{[\s\S]*?padding:\s*14px;/)
})

test('iframe 非全屏提供新建会话入口，状态面板支持外部点击关闭', () => {
  const viewSource = readFileSync(new URL('../../src/views/AgentView.vue', import.meta.url), 'utf8')
  const chatSource = readFileSync(
    new URL('../../src/components/AgentChatComponent.vue', import.meta.url),
    'utf8'
  )

  assert.match(viewSource, /class="embed-new-chat-btn agent-nav-btn"/)
  assert.match(viewSource, /@click="handleCreateNewChat"/)
  assert.match(chatSource, /useOutsidePointerdown\(statePanelOpen,\s*\[statePanelRef, statePanelTriggerRef, contextUsageTriggerRef\]\)/)
})

test('窄屏顶部保留右侧模式按钮并允许左侧标题省略', () => {
  const chatSource = readFileSync(
    new URL('../../src/components/AgentChatComponent.vue', import.meta.url),
    'utf8'
  )
  const viewSource = readFileSync(new URL('../../src/views/AgentView.vue', import.meta.url), 'utf8')

  assert.match(chatSource, /\.header__right\s*\{[\s\S]*?flex:\s*0 0 auto;[\s\S]*?min-width:\s*max-content;/)
  assert.match(chatSource, /@media \(max-width: 768px\)\s*\{[\s\S]*?\.chat-header\s*\{[\s\S]*?padding-inline:\s*4px;/)
  assert.match(viewSource, /\.embed-title\s*\{[\s\S]*?text-overflow:\s*ellipsis;/)
})
