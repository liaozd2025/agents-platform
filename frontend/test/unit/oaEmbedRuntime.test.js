import assert from 'node:assert/strict'
import test from 'node:test'
import { Buffer } from 'node:buffer'
import { createPinia, setActivePinia } from 'pinia'
import { createRenderer, h, ref } from 'vue'
import { routerKey, routeLocationKey } from 'vue-router'
import { createTestServer } from '../helpers/createTestServer.js'

/** 仅替换浏览器 history 和页面加载，保留真实路由表与全局导航守卫。 */
function routeTestBoundary() {
  return {
    name: 'oa-route-browser-boundary',
    enforce: 'pre',
    transform(source, id) {
      if (!id.endsWith('/src/app/router/index.js')) return
      return source
        .replace('createRouter, createWebHistory', 'createRouter, createMemoryHistory as createWebHistory')
        .replace(/\(\) => import\([^\n]+?\)/g, '() => Promise.resolve({ render: () => null })')
    }
  }
}

test('真实 OA 路由保留知识库新旧路径、评估返回及 root/child 来源参数', async () => {
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  const server = await createTestServer({ server: { middlewareMode: true }, plugins: [routeTestBoundary()] })
  setActivePinia(createPinia())
  try {
    const { default: router } = await server.ssrLoadModule('/src/app/router/index.js')
    const { useUserStore } = await server.ssrLoadModule('/src/modules/identity/model/user.js')
    const { createSessionLinkNavigator } = await server.ssrLoadModule('/src/modules/session/model/sessionLinks.js')
    const user = useUserStore()
    user.token = 'test-session'
    user.userId = 'user'
    user.effectivePermissions = ['agent:use', 'knowledge_base:read', 'knowledge_evaluation:manage']
    await router.push('/embed/extensions')
    // KnowledgeCatalog 的真实导航目标。
    await router.push('/extensions/knowledge-bases/kb-test')
    assert.equal(router.currentRoute.value.name, 'EmbedExtensionKnowledgeBaseDetail')
    assert.equal(router.currentRoute.value.path, '/embed/extensions/knowledge-bases/kb-test')
    await router.push('/embed/extensions/knowledgebase/kb-test')
    assert.equal(router.currentRoute.value.name, 'EmbedExtensionKnowledgeBaseDetail')
    await router.push({ name: 'EmbedExtensionEvaluationBenchmarkDetail', params: { kbId: 'kb-test', datasetId: 'dataset' } })
    assert.equal(router.currentRoute.value.path, '/embed/extensions/knowledge-bases/kb-test/evaluation/dataset')
    await router.push('/embed/extensions/knowledgebase/kb-test/evaluation/dataset')
    assert.equal(router.currentRoute.value.name, 'EmbedExtensionEvaluationBenchmarkDetail')
    // EvaluationBenchmarkDetailView 返回按钮的真实命名路由。
    await router.push({ name: 'ExtensionKnowledgeBaseDetail', params: { kbId: 'kb-test' }, query: { section: 'evaluation' } })
    assert.equal(router.currentRoute.value.name, 'EmbedExtensionKnowledgeBaseDetail')
    assert.deepEqual(router.currentRoute.value.query, { section: 'evaluation' })
    const errors = []
    const navigator = createSessionLinkNavigator({ router, resolveTarget: async (target) => target,
      isActive: () => true, openTarget() {}, onError: (error) => errors.push(error) })
    for (const parent of [null, 'root']) {
      await navigator.navigate({ tree_root_session_id: 'root', session_id: parent ? 'child' : 'root', parent_session_id: parent })
      assert.equal(router.currentRoute.value.name, 'EmbedAgentWithThreadId')
      assert.equal(router.currentRoute.value.path, '/embed/root')
      assert.deepEqual(router.currentRoute.value.query, parent ? { member_session_id: 'child' } : {})
    }
    await router.push({ path: '/agent/root', query: { member_session_id: 'child' }, hash: '#answer' })
    assert.equal(router.currentRoute.value.fullPath, '/embed/root?member_session_id=child#answer')
    assert.deepEqual(errors, [])
    assert.equal(user.token, 'test-session')
  } finally { await server.close(); delete globalThis.localStorage }
})

const node = () => ({ children: [], parent: null })
const renderer = createRenderer({
  createElement: node, createText: node, createComment: node,
  insert(child, parent) { child.parent = parent; parent.children.push(child) },
  remove() {}, setText() {}, setElementText() {}, parentNode: (item) => item.parent,
  nextSibling: () => null, patchProp() {}
})

test('真实 OA bridge 在续期、401 重新授权和卸载时清理 Store 并通知父页面', async (t) => {
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  const messages = [], renewalCallbacks = []
  let messageListener
  const browserWindow = {
    location: { pathname: '/embed' },
    parent: { postMessage: (message) => messages.push(message) },
    addEventListener: (_type, callback) => { messageListener = callback },
    removeEventListener: () => { messageListener = null },
    setTimeout: (callback) => { renewalCallbacks.push(callback); return renewalCallbacks.length },
    clearTimeout() {}
  }
  const server = await createTestServer({ server: { middlewareMode: true },
    define: { 'import.meta.env.VITE_YUXI_EMBED_ALLOWED_ORIGINS': JSON.stringify('https://oa.example.test') } })
  globalThis.window = browserWindow
  setActivePinia(createPinia())
  let app
  try {
    const { useOAEmbedBridge } = await server.ssrLoadModule('/src/app/composables/useOAEmbedBridge.js')
    const { requestOAEmbedAuthentication } = await server.ssrLoadModule('/src/shared/lib/oaEmbedSession.js')
    const { useChatThreadsStore } = await server.ssrLoadModule('/src/modules/session/model/chatThreads.js')
    const { useUserStore } = await server.ssrLoadModule('/src/modules/identity/model/user.js')
    const { authApi } = await server.ssrLoadModule('/src/apis/auth_api.js')
    const token = `header.${Buffer.from(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 })).toString('base64url')}.signature`
    t.mock.method(authApi, 'exchangeOAAccount', async () => ({ access_token: token }))
    t.mock.method(authApi, 'getCurrentUser', async () => ({ id: 'user', uid: 'oa:user', effective_permissions: ['agent:use'] }))
    const route = { matched: [{ meta: { embed: true, requiredPermission: 'agent:use' } }] }
    let state
    app = renderer.createApp({ setup() { state = useOAEmbedBridge(ref(true)); return () => h('div') } })
    app.provide(routerKey, { replace() {} })
    app.provide(routeLocationKey, route)
    app.mount(node())
    const store = useChatThreadsStore()
    const user = useUserStore()
    const login = () => messageListener({ source: browserWindow.parent, origin: 'https://oa.example.test',
      data: { type: 'login-params', data: { userInfo: { account: 'user' } } } })
    await login()
    assert.equal(state.isAuthorized.value, true)
    store.upsertThread({ id: 'old', yuxi: {} })
    renewalCallbacks.at(-1)()
    assert.equal(state.isAuthorized.value, false)
    assert.deepEqual(store.threads, [])
    assert.equal(user.token, '')
    assert.equal(messages.at(-1).type, 'request-login-params')
    await login()
    assert.equal(state.isAuthorized.value, true)
    const previousCount = messages.length
    assert.equal(requestOAEmbedAuthentication(), true)
    assert.equal(messages.length, previousCount + 1)
    assert.equal(messages.at(-1).type, 'request-login-params')
    await login()
    store.upsertThread({ id: 'last', yuxi: {} })
    app.unmount()
    app = null
    assert.equal(messageListener, null)
    assert.deepEqual(store.threads, [])
    assert.equal(user.token, '')
    assert.equal(requestOAEmbedAuthentication(), false)
  } finally { app?.unmount(); await server.close(); delete globalThis.window; delete globalThis.localStorage }
})
