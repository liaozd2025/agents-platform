import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
const shell = readFileSync(new URL('../../src/app/layouts/AppLayout.vue', import.meta.url), 'utf8')
const session = readFileSync(new URL('../../src/modules/session/ui/SessionWorkspace.vue', import.meta.url), 'utf8')
test('OA 历史抽屉独立滚动，授权前不挂载会话导航', () => {
  assert.match(shell, /v-if="isEmbedded && embedAuthorized" v-model:open="embedHistoryOpen"/)
  assert.match(shell, /minHeight: '0', overflow: 'hidden'/)
  assert.match(shell, /class="embed-session-history" :visible="embedHistoryOpen"/)
})
test('OA 窄窗提供新建与历史入口，并保留模式和关闭控制', () => {
  for (const label of ['新建会话', '会话历史', 'AI 助手显示模式', '关闭 AI 助手']) assert.ok(shell.includes(label))
  assert.match(shell, /v-if="isEmbedded && embedAuthorized && !showSidebar"/)
})
test('OA 全屏首屏隐藏会话标题，输入和欢迎区居中', () => {
  assert.ok(session.includes('!(isOAEmbedded && !currentChatId)'))
  assert.match(session, /\.chat\.is-oa-fullscreen \.bottom\.start-screen \{ top: 50%; left: 50%;/)
  assert.match(session, /\.header__right \{ flex: 0 0 auto; min-width: max-content;/)
})
