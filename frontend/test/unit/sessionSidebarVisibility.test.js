import assert from 'node:assert/strict'
import test from 'node:test'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import Antd from 'ant-design-vue'
import { createTestServer } from '../helpers/createTestServer.js'

test('基于上游归档规则，侧栏同时隐藏自动 root、子会话和归档项', async (t) => {
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  globalThis.document = { compatMode: 'CSS1Compat', documentElement: { classList: { add() {}, remove() {} } } }
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' })
  const server = await createTestServer({ server: { middlewareMode: true } })
  const pinia = createPinia()
  setActivePinia(pinia)
  try {
    const { useChatThreadsStore } = await server.ssrLoadModule('/src/modules/session/model/chatThreads.js')
    const store = useChatThreadsStore()
    const { threadApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
    t.mock.method(threadApi, 'getHistoryThreads', async (_agent, _limit, _after, { isPinned } = {}) => ({
      data: isPinned ? [] : [
        { id: 'normal', status: 'idle', yuxi: { title: 'VISIBLE_USER_ROOT' } },
        { id: 'archived', status: 'idle', yuxi: { title: 'HIDDEN_ARCHIVED', archived: false } }
      ], has_more: false
    }))
    await store.loadThreads()
    store.upsertThread({ id: 'generated-root', status: 'idle', yuxi: { title: 'HIDDEN_INDEPENDENT_ROOT', parent_session_id: null } })
    store.upsertThread({ id: 'child', status: 'idle', yuxi: { title: 'HIDDEN_CHILD', parent_session_id: 'normal' } })
    store.upsertThread({ id: 'archived', status: 'idle', yuxi: { title: 'HIDDEN_ARCHIVED', archived: true } })
    const { default: Navigation } = await server.ssrLoadModule('/src/modules/session/ui/SessionNavigation.vue')
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/agent/:thread_id', name: 'AgentCompWithThreadId', component: Navigation }] })
    await router.push('/agent/archived')
    store.setCurrentThreadId('archived')
    const app = createSSRApp({ render: () => h(Navigation) })
    app.use(pinia).use(router).use(Antd)
    const html = await renderToString(app)
    assert.match(html, /VISIBLE_USER_ROOT/)
    assert.doesNotMatch(html, /HIDDEN_CHILD|HIDDEN_ARCHIVED|HIDDEN_INDEPENDENT_ROOT/)
    assert.equal(store.currentThread.id, 'archived')
  } finally {
    disposePinia(pinia)
    await server.close()
    delete globalThis.localStorage
    delete globalThis.document
    delete globalThis.getComputedStyle
  }
})
