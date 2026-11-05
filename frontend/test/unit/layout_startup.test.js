import assert from 'node:assert/strict'
import test from 'node:test'
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { createRenderer, h, ssrContextKey } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import { createTestServer } from '../helpers/createTestServer.js'

test('布局导航不等待品牌或知识库，卸载后清理状态同步计时器', async (t) => {
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  globalThis.window = { location: { pathname: '/' }, addEventListener() {}, removeEventListener() {} }
  const server = await createTestServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom'
  })
  const pinia = createPinia()
  setActivePinia(pinia)
  let app
  try {
    const calls = []
    for (const [module, storeName, method, pending] of [
      ['settings/model/info', 'useInfoStore', 'loadInfoConfig', true],
      ['settings/model/config', 'useConfigStore', 'refreshConfig', false],
      ['session/model/chatThreads', 'useChatThreadsStore', 'loadThreads', false],
      ['projects/model/projects', 'useProjectsStore', 'loadProjects', false]
    ]) {
      const exports = await server.ssrLoadModule(`/src/modules/${module}.js`)
      const store = exports[storeName]()
      t.mock.method(store, method, () => {
        calls.push(method)
        return pending ? new Promise(() => {}) : Promise.resolve()
      })
    }
    const { useUserStore } = await server.ssrLoadModule('/src/modules/identity/model/user.js')
    useUserStore().effectivePermissions = ['agent:use', 'knowledge_base:read', 'system_config:manage', 'system_task:manage']
    const { backgroundJobsApi } = await server.ssrLoadModule('/src/apis/background_jobs.js')
    let statusCounts = {}
    t.mock.method(backgroundJobsApi, 'fetchJobs', async () => ({
      jobs: [], summary: { total: 13, status_counts: statusCounts }
    }))
    const { useBackgroundJobsStore } = await server.ssrLoadModule('/src/modules/background-jobs/model/jobs.js')
    const jobsStore = useBackgroundJobsStore()
    const { useAgentStore } = await server.ssrLoadModule('/src/modules/agents/model/agent.js')
    useAgentStore().isInitialized = true
    const { default: Layout } = await server.ssrLoadModule('/src/app/layouts/AppLayout.vue')
    const renderer = createRenderer({
      createElement: () => ({}),
      createText: () => ({}),
      createComment: () => ({}),
      insert() {},
      remove() {},
      setText() {},
      setElementText() {},
      patchProp() {},
      parentNode: () => null,
      nextSibling: () => null
    })
    app = renderer.createApp({ ...Layout, render: () => h('div') })
    app.provide(ssrContextKey, { modules: new Set() })
    app.use(pinia)
    app.use(createRouter({ history: createMemoryHistory(), routes: [] }))
    const { useKnowledgeBaseStore } = await server.ssrLoadModule('/src/modules/knowledge/model/knowledgeBase.js')
    const knowledgeBase = app.runWithContext(() => useKnowledgeBaseStore())
    t.mock.method(knowledgeBase, 'loadKnowledgeBases', () => {
      calls.push('loadKnowledgeBases')
      return new Promise(() => {})
    })
    const interval = t.mock.method(globalThis, 'setInterval', () => 123)
    const clear = t.mock.method(globalThis, 'clearInterval', () => {})
    const layout = app.mount({})
    assert.equal(layout.$.setupState.activeTaskCount, 0)
    await new Promise((resolve) => setTimeout(resolve, 0))
    assert.ok(calls.includes('loadInfoConfig'))
    assert.ok(calls.includes('loadKnowledgeBases'))
    assert.ok(calls.includes('loadThreads'))
    assert.ok(calls.includes('loadProjects'))
    assert.ok(calls.includes('refreshConfig'))
    assert.equal(interval.mock.callCount(), 1)
    // 直接求值真实布局computed，不能用仅渲染空div掩盖Store契约缺失。
    statusCounts = { pending: 2, running: 3, failed: 8 }
    await jobsStore.loadJobs()
    assert.equal(layout.$.setupState.activeTaskCount, 5)
    statusCounts = { success: 13 }
    await jobsStore.loadJobs()
    assert.equal(layout.$.setupState.activeTaskCount, 0)
    app.unmount()
    app = null
    assert.equal(clear.mock.calls[0].arguments[0], 123)
  } finally {
    app?.unmount()
    disposePinia(pinia)
    await server.close()
    delete globalThis.window
  }
})
