import assert from 'node:assert/strict'
import test from 'node:test'

import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'

const TOKEN = 'jwt.payload.sig'

/**
 * 用 Map 桩替换浏览器存储，便于断言「跨会话」与「仅本会话」两侧写入、清理的互斥关系。
 * @param {Record<string, string>} initial 初始键值，用于模拟上一次登录留下的残留
 */
function createStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
    has: (key) => values.has(key),
    read: (key) => values.get(key)
  }
}

/**
 * 在受控存储环境下加载真实的 user store 并执行用例。
 * 令牌恢复发生在 store 初始化时，因此必须先装好存储桩再调用 useUserStore()。
 * @param {{ local?: Record<string, string>, session?: Record<string, string> }} seed 两个存储的初始内容
 * @param {(userStore: object, storage: { local: object, session: object }) => Promise<void>} run 用例主体
 */
async function withUserStore(seed, run) {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  const originalLocal = globalThis.localStorage
  const originalSession = globalThis.sessionStorage
  globalThis.localStorage = createStorage(seed.local)
  globalThis.sessionStorage = createStorage(seed.session)
  try {
    setActivePinia(createPinia())
    const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
    return await run(useUserStore(), {
      local: globalThis.localStorage,
      session: globalThis.sessionStorage
    })
  } finally {
    globalThis.localStorage = originalLocal
    globalThis.sessionStorage = originalSession
    await server.close()
  }
}

test('勾选保持登录：令牌写入 localStorage，且清空 sessionStorage', async () => {
  await withUserStore({}, async (userStore, storage) => {
    userStore.persistToken(TOKEN, true)

    assert.equal(storage.local.read('user_token'), TOKEN)
    // 两种存储必须互斥，否则关掉浏览器后无法判断应以哪一份为准
    assert.equal(storage.session.has('user_token'), false)
  })
})

test('未勾选保持登录：令牌只写入 sessionStorage', async () => {
  await withUserStore({}, async (userStore, storage) => {
    userStore.persistToken(TOKEN, false)

    assert.equal(storage.session.read('user_token'), TOKEN)
    assert.equal(storage.local.has('user_token'), false)
  })
})

test('勾选状态下不传参默认按保持登录处理（OA 免登、初始化管理员路径）', async () => {
  await withUserStore({}, async (userStore, storage) => {
    userStore.persistToken(TOKEN)

    assert.equal(storage.local.read('user_token'), TOKEN)
    assert.equal(storage.session.has('user_token'), false)
  })
})

test('由勾选切换为不勾选：旧 localStorage 令牌被清理，不再自动免登录', async () => {
  await withUserStore({}, async (userStore, storage) => {
    userStore.persistToken('old-token', true)
    userStore.persistToken(TOKEN, false)

    assert.equal(storage.local.has('user_token'), false)
    assert.equal(storage.session.read('user_token'), TOKEN)
  })
})

test('首屏恢复优先取跨会话令牌，并清理 sessionStorage 残留', async () => {
  await withUserStore(
    { local: { user_token: 'persistent-token' }, session: { user_token: 'session-token' } },
    async (userStore, storage) => {
      assert.equal(userStore.token, 'persistent-token')
      assert.equal(storage.session.has('user_token'), false)
    }
  )
})

test('仅存在会话令牌时可恢复，两处皆空时恢复为空串', async () => {
  await withUserStore({ session: { user_token: 'session-token' } }, async (userStore) => {
    assert.equal(userStore.token, 'session-token')
  })

  await withUserStore({}, async (userStore) => {
    assert.equal(userStore.token, '')
  })
})
