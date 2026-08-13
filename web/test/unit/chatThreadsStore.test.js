import assert from 'node:assert/strict'
import test from 'node:test'

import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'

globalThis.localStorage = {
  getItem: () => null,
  setItem() {},
  removeItem() {}
}

test('OA 重新授权清空会话列表但保留路由中的 thread id', async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  try {
    setActivePinia(createPinia())
    const { useChatThreadsStore } = await server.ssrLoadModule('/src/stores/chatThreads.js')
    const store = useChatThreadsStore()
    store.setCurrentThreadId('thread-1')
    store.upsertThread({ id: 'thread-1', title: '原会话' })

    store.reset()

    assert.equal(store.currentThreadId, 'thread-1')
    assert.deepEqual(store.threads, [])
  } finally {
    await server.close()
  }
})
