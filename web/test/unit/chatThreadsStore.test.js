import assert from 'node:assert/strict'
import test from 'node:test'

import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'

let storedToken = null

globalThis.localStorage = {
  getItem: () => storedToken,
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

test('状态同步覆盖已加载的分页会话', async () => {
  const originalFetch = globalThis.fetch
  let requestedUrl = ''
  let fetchedThreads = []
  storedToken = 'test-token'
  globalThis.fetch = async (url) => {
    requestedUrl = String(url)
    return new Response(JSON.stringify(fetchedThreads), {
      status: 200,
      headers: { 'Content-Type': 'application/json' }
    })
  }
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })

  try {
    setActivePinia(createPinia())
    const { useChatThreadsStore } = await server.ssrLoadModule('/src/stores/chatThreads.js')
    const store = useChatThreadsStore()
    for (let index = 0; index < 101; index += 1) {
      store.upsertThread({
        id: `thread-${index}`,
        thread_status: 'loading',
        is_pinned: index === 0
      })
    }
    fetchedThreads = store.threads.map((thread) =>
      thread.id === 'thread-100' ? { ...thread, thread_status: 'done' } : thread
    )

    await store.syncThreadStatuses()

    assert.match(requestedUrl, /limit=102/)
    assert.equal(store.threads.find((thread) => thread.id === 'thread-100').thread_status, 'done')
  } finally {
    storedToken = null
    globalThis.fetch = originalFetch
    await server.close()
  }
})
