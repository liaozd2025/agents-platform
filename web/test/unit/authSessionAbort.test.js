import assert from 'node:assert/strict'
import test from 'node:test'

import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'

const storageValues = new Map([['user_token', 'old-token']])
globalThis.localStorage = {
  getItem: (key) => storageValues.get(key) ?? null,
  setItem: (key, value) => storageValues.set(key, String(value)),
  removeItem: (key) => storageValues.delete(key)
}

test('切换 OA token 时取消旧身份的未完请求', async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  const originalFetch = globalThis.fetch
  try {
    setActivePinia(createPinia())
    const { apiGet } = await server.ssrLoadModule('/src/apis/base.js')
    const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
    const userStore = useUserStore()
    globalThis.fetch = (_url, { signal }) =>
      new Promise((_resolve, reject) => {
        signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
      })

    const oldRequest = apiGet('/api/agent')
    userStore.logout()

    await assert.rejects(oldRequest, { name: 'AbortError' })
  } finally {
    globalThis.fetch = originalFetch
    await server.close()
  }
})
