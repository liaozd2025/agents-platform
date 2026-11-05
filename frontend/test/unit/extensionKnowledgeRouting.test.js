import assert from 'node:assert/strict'
import test from 'node:test'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { createRouter, createMemoryHistory, RouterView, useRoute } from 'vue-router'
import Antd from 'ant-design-vue'
import { createTestServer } from '../helpers/createTestServer.js'

test('扩展列表正确让位给知识库详情、编辑与评估路由，兼容 OA 路径', async (t) => {
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  globalThis.document = { compatMode: 'CSS1Compat', documentElement: { classList: { add() {}, remove() {} } } }
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' })
  const server = await createTestServer({ server: { middlewareMode: true } })
  t.after(async () => {
    await server.close()
    delete globalThis.localStorage
    delete globalThis.document
    delete globalThis.getComputedStyle
  })
  const { default: ExtensionsView } = await server.ssrLoadModule('/src/pages/ExtensionsView.vue')
  const Detail = { setup() {
    const route = useRoute()
    return () => h('article', { 'data-kb-detail': route.params.kbId }, `detail:${route.query.action || route.params.datasetId || 'files'}`)
  } }
  for (const [base, segment] of [['/extensions', 'knowledge-bases'], ['/embed/extensions', 'knowledgebase']]) {
    for (const suffix of ['', '?action=edit', '/evaluation/dataset-test']) {
      await t.test(`${base}/${segment}/kb-test${suffix}`, async () => {
        const pinia = createPinia()
        setActivePinia(pinia)
        try {
          const router = createRouter({ history: createMemoryHistory(), routes: [{
            path: base, component: ExtensionsView, children: [
              { path: `${segment}/:kbId`, component: Detail },
              { path: `${segment}/:kbId/evaluation/:datasetId`, component: Detail }
            ]
          }] })
          await router.push(`${base}/${segment}/kb-test${suffix}`)
          await router.isReady()
          const app = createSSRApp({ render: () => h(RouterView) })
          app.use(pinia).use(router).use(Antd)
          const html = await renderToString(app)
          assert.match(html, /data-kb-detail="kb-test"/)
          assert.match(html, new RegExp(`detail:${suffix.includes('edit') ? 'edit' : suffix.includes('evaluation') ? 'dataset-test' : 'files'}`))
          assert.doesNotMatch(html, /智能体扩展视图切换/)
        } finally { disposePinia(pinia) }
      })
    }
  }
})
