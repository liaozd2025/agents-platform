import assert from 'node:assert/strict'
import test from 'node:test'
import { createRouter, createMemoryHistory } from 'vue-router'
import { createTestServer } from '../helpers/createTestServer.js'

test('不同会话获得独立面板实例，重返会话复用其自身实例', async () => {
  const server = await createTestServer({ server: { middlewareMode: true }, appType: 'custom' })
  try {
    const { createSessionRouteCache } = await server.ssrLoadModule('/src/app/layouts/sessionRouteCache.ts')
    const cache = createSessionRouteCache(createRouter({ history: createMemoryHistory(), routes: [] }))
    const route = (id) => ({ name: 'AgentCompWithThreadId', params: { thread_id: id }, query: {}, path: `/agent/${id}` })
    const first = cache.key(route('first'))
    assert.notEqual(cache.key(route('second')), first)
    assert.equal(cache.key(route('first')), first)
  } finally { await server.close() }
})
