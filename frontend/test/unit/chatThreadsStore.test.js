import assert from 'node:assert/strict'
import test from 'node:test'
import { setImmediate } from 'node:timers'

import { createPinia, setActivePinia } from 'pinia'
import { createTestServer } from '../helpers/createTestServer.js'

globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {}
}

test('切换身份清空详情与历史，迟到列表不能回写，新账号不等待旧请求', async () => {
  const server = await createTestServer({ server: { middlewareMode: true }, appType: 'custom' })
  setActivePinia(createPinia())
  try {
    const { useUserStore } = await server.ssrLoadModule('/src/modules/identity/model/user.js')
    const { useChatThreadsStore } = await server.ssrLoadModule('/src/modules/session/model/chatThreads.js')
    const { threadApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
    const user = useUserStore()
    const store = useChatThreadsStore()
    user.userId = 'old-user'
    store.upsertThread({ id: 'old-detail', yuxi: {} })
    store.setCurrentThreadId('old-detail')
    store.setThreadCreationInFlight(true)
    let resolveOldPage
    threadApi.getHistoryThreads = async (_agent, _limit, _after, { isPinned } = {}) => {
      if (isPinned) return { data: [], has_more: false }
      return new Promise((resolve) => { resolveOldPage = resolve })
    }
    const oldLoad = store.loadThreads()
    await new Promise(setImmediate)
    user.userId = 'new-user'
    assert.deepEqual(store.threads, [])
    assert.deepEqual(store.historyThreads, [])
    assert.equal(store.currentThreadId, null)
    assert.equal(store.threadCreationInFlight, false)
    threadApi.getHistoryThreads = async (_agent, _limit, after, { isPinned } = {}) => {
      assert.equal(after, isPinned ? null : undefined)
      return { data: isPinned ? [] : [{ id: 'new-user-thread', yuxi: {} }], has_more: false }
    }
    await store.loadThreads()
    resolveOldPage({ data: [{ id: 'old-user-thread', yuxi: {} }], last_id: 'old-cursor', has_more: true })
    await oldLoad
    assert.deepEqual(store.historyThreads.map((thread) => thread.id), ['new-user-thread'])
    assert.deepEqual(store.threads.map((thread) => thread.id), ['new-user-thread'])
    assert.equal(store.hasMoreThreads, false)
  } finally { await server.close() }
})

test('reset 使旧翻页、排队创建与迟到写入失效，保留新会话状态', async () => {
  const server = await createTestServer({ server: { middlewareMode: true }, appType: 'custom' })
  setActivePinia(createPinia())
  try {
    const { useChatThreadsStore } = await server.ssrLoadModule('/src/modules/session/model/chatThreads.js')
    const { threadApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
    const store = useChatThreadsStore()
    let resolveOldPage
    let creations = 0
    threadApi.getHistoryThreads = () => new Promise((resolve) => { resolveOldPage = resolve })
    threadApi.createThread = async () => { creations++; return { id: 'new', yuxi: {} } }
    const oldLoad = store.loadMoreThreads()
    await new Promise(setImmediate)
    const oldCreate = store.createThread('old-agent')
    store.reset()
    assert.equal(store.isLoadingMoreThreads, false)
    await store.createThread('new-agent')
    resolveOldPage({ data: [{ id: 'old', yuxi: {} }], has_more: true })
    await Promise.all([oldLoad, oldCreate])
    assert.equal(creations, 1)
    assert.deepEqual(store.historyThreads.map((thread) => thread.id), ['new'])

    for (const operation of ['createThread', 'updateThread', 'markThreadViewed']) {
      let resolveResponse
      threadApi[operation] = () => new Promise((resolve) => { resolveResponse = resolve })
      const pending = operation === 'updateThread'
        ? store.updateThread('old', 'rename') : store[operation]('old')
      await new Promise(setImmediate)
      store.reset()
      store.upsertThread({ id: 'current-detail', yuxi: {} })
      resolveResponse({ id: 'old', yuxi: {} })
      await pending
      assert.deepEqual(store.threads.map((thread) => thread.id), ['current-detail'], operation)
      assert.deepEqual(store.historyThreads, [])
    }
  } finally { await server.close() }
})

test('线程创建期间共享 Store 拒绝外层切换，创建结果可显式提交', async () => {
  const server = await createTestServer({ server: { middlewareMode: true }, appType: 'custom' })
  setActivePinia(createPinia())
  try {
    const { useChatThreadsStore } = await server.ssrLoadModule(
      '/src/modules/session/model/chatThreads.js'
    )
    const store = useChatThreadsStore()
    store.setCurrentThreadId('thread-before')
    store.setThreadCreationInFlight(true)

    assert.equal(store.setCurrentThreadId('thread-sidebar'), false)
    assert.equal(store.currentThreadId, 'thread-before')
    assert.equal(store.setCurrentThreadId('thread-created', { force: true }), true)
    assert.equal(store.currentThreadId, 'thread-created')

    store.setThreadCreationInFlight(false)
  } finally {
    await server.close()
  }
})

test('Project 删除后 Store 只移除对应 Session 并清空当前选择', async () => {
  const server = await createTestServer({ server: { middlewareMode: true }, appType: 'custom' })
  setActivePinia(createPinia())
  try {
    const { useChatThreadsStore } = await server.ssrLoadModule(
      '/src/modules/session/model/chatThreads.js'
    )
    const store = useChatThreadsStore()
    store.threads = [
      { id: 'a', yuxi: { project_id: 'project-a' } },
      { id: 'b', yuxi: { project_id: 'project-b' } }
    ]
    store.setCurrentThreadId('a')
    assert.deepEqual(store.removeThreadsByProject('project-a'), ['a'])
    assert.deepEqual(store.threads, [{ id: 'b', yuxi: { project_id: 'project-b' } }])
    assert.equal(store.currentThreadId, null)
  } finally {
    await server.close()
  }
})

for (const mutation of ['pin', 'archive', 'create', 'detail']) {
  test(`列表游标不被 ${mutation} 操作改变，下一页保留边界 Session`, async () => {
    const server = await createTestServer({
      server: { middlewareMode: true, hmr: false },
      appType: 'custom'
    })
    setActivePinia(createPinia())
    try {
      const { threadApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
      const calls = []
      threadApi.getHistoryThreads = async (_agentId, limit, after, { isPinned } = {}) => {
        if (isPinned) return { data: [], last_id: null, has_more: false }
        calls.push({ limit, after })
        return !after
          ? {
              object: 'list',
              data: [
                { id: 'first', yuxi: { is_pinned: true } },
                { id: 'boundary', yuxi: {} }
              ],
              last_id: 'boundary',
              has_more: true
            }
          : { object: 'list', data: [{ id: 'next', yuxi: {} }], last_id: 'next', has_more: false }
      }
      threadApi.updateThread = async () => ({ id: 'boundary', yuxi: { is_pinned: true } })
      threadApi.archiveThread = async () => ({ id: 'boundary', yuxi: { archived: true } })
      threadApi.createThread = async () => ({ id: 'created', yuxi: {} })
      const { useChatThreadsStore } = await server.ssrLoadModule(
        '/src/modules/session/model/chatThreads.js'
      )
      const store = useChatThreadsStore()
      await store.loadThreads()
      if (mutation === 'pin') await store.updateThread('boundary', null, true)
      if (mutation === 'archive') await store.archiveThread('boundary')
      if (mutation === 'create') await store.createThread('agent')
      if (mutation === 'detail')
        store.upsertThread({ id: 'child', yuxi: { parent_session_id: 'first' } })
      await store.loadMoreThreads()
      assert.deepEqual(calls, [
        { limit: 100, after: undefined },
        { limit: 100, after: 'boundary' }
      ])
      assert.equal(
        store.threads.some((session) => session.id === 'next'),
        true
      )
      assert.equal(store.hasMoreThreads, false)
    } finally {
      await server.close()
    }
  })
}

test('下一页与归档串行执行，归档结果不会重新出现在列表', async () => {
  const server = await createTestServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom'
  })
  setActivePinia(createPinia())
  try {
    const { threadApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
    let resolvePage
    const operations = []
    threadApi.getHistoryThreads = async (_agentId, _limit, after, { isPinned } = {}) => {
      if (isPinned) return { data: [], last_id: null, has_more: false }
      if (!after)
        return { data: [{ id: 'boundary', yuxi: {} }], last_id: 'boundary', has_more: true }
      operations.push('page')
      return new Promise((resolve) => {
        resolvePage = resolve
      })
    }
    threadApi.archiveThread = async () => {
      operations.push('archive')
    }
    const { useChatThreadsStore } = await server.ssrLoadModule(
      '/src/modules/session/model/chatThreads.js'
    )
    const store = useChatThreadsStore()
    await store.loadThreads()
    const loading = store.loadMoreThreads()
    await new Promise(setImmediate)
    const archiving = store.archiveThread('boundary')
    assert.deepEqual(operations, ['page'])
    resolvePage({ data: [{ id: 'next', yuxi: {} }], last_id: 'next', has_more: false })
    await Promise.all([loading, archiving])
    assert.deepEqual(operations, ['page', 'archive'])
    assert.deepEqual(
      store.threads.map((session) => session.id),
      ['next']
    )
  } finally {
    await server.close()
  }
})

test('较老的置顶 Session 始终展示，刷新状态不改变普通列表游标', async () => {
  const server = await createTestServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom'
  })
  setActivePinia(createPinia())
  try {
    const { threadApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
    let pinnedStatus = 'idle'
    const nextCursors = []
    threadApi.getHistoryThreads = async (_agentId, _limit, after, { isPinned } = {}) => {
      if (isPinned)
        return {
          data: [{ id: 'old-pin', status: pinnedStatus, yuxi: { is_pinned: true } }],
          last_id: 'old-pin',
          has_more: false
        }
      if (after) nextCursors.push(after)
      return after
        ? { data: [{ id: 'next', yuxi: {} }], last_id: 'next', has_more: false }
        : { data: [{ id: 'newest', yuxi: {} }], last_id: 'newest', has_more: true }
    }
    const { useChatThreadsStore } = await server.ssrLoadModule(
      '/src/modules/session/model/chatThreads.js'
    )
    const store = useChatThreadsStore()
    await store.loadThreads()
    assert.deepEqual(
      store.threads.map((item) => item.id),
      ['newest', 'old-pin']
    )
    pinnedStatus = 'in_progress'
    await store.syncThreadStatuses()
    assert.equal(store.threads.find((item) => item.id === 'old-pin').status, 'in_progress')
    await store.loadMoreThreads()
    assert.deepEqual(nextCursors, ['newest'])
    assert.deepEqual(
      store.threads.map((item) => item.id),
      ['newest', 'old-pin', 'next']
    )
  } finally {
    await server.close()
  }
})

test('后台状态读取与改名串行，迟到资源不会覆盖已保存的标题', async () => {
  const server = await createTestServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom'
  })
  setActivePinia(createPinia())
  try {
    const { threadApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
    let resolvePage
    const writes = []
    threadApi.getHistoryThreads = async (_agentId, _limit, _after, { isPinned } = {}) => {
      if (isPinned) return { data: [], last_id: null, has_more: false }
      return new Promise((resolve) => {
        resolvePage = resolve
      })
    }
    threadApi.updateThread = async () => {
      writes.push('rename')
      return { id: 'session', yuxi: { title: '已保存的新标题' } }
    }
    const { useChatThreadsStore } = await server.ssrLoadModule(
      '/src/modules/session/model/chatThreads.js'
    )
    const store = useChatThreadsStore()
    store.upsertThread({ id: 'session', yuxi: { title: '旧标题' } })
    const syncing = store.syncThreadStatuses()
    await new Promise(setImmediate)
    const renamed = store.updateThread('session', '已保存的新标题')
    assert.deepEqual(writes, [])
    resolvePage({
      data: [{ id: 'session', status: 'idle', yuxi: { title: '旧标题' } }],
      has_more: false
    })
    await Promise.all([syncing, renamed])
    assert.deepEqual(writes, ['rename'])
    assert.equal(store.threads[0].yuxi.title, '已保存的新标题')
  } finally {
    await server.close()
  }
})


test('按上游规则保留当前归档详情，但不把未打开的归档会话加入缓存', async () => {
  const server = await createTestServer({ server: { middlewareMode: true }, appType: 'custom' })
  setActivePinia(createPinia())
  try {
    const { useChatThreadsStore } = await server.ssrLoadModule('/src/modules/session/model/chatThreads.js')
    const store = useChatThreadsStore()
    store.upsertThread({ id: 'archived-other', yuxi: { archived: true } })
    assert.deepEqual(store.threads, [])
    store.setCurrentThreadId('archived-current')
    store.upsertThread({ id: 'archived-current', yuxi: { archived: true } })
    assert.equal(store.currentThread.id, 'archived-current')
    store.upsertThread({ id: 'active', yuxi: { archived: false } })
    store.upsertThread({ id: 'active', yuxi: { archived: true } })
    assert.equal(store.threads.find(thread => thread.id === 'active').yuxi.archived, true)
  } finally { await server.close() }
})


test('自动 root 详情缓存不进入历史，用户新建与分页会话仍显示', async (t) => {
  const server = await createTestServer({ server: { middlewareMode: true }, appType: 'custom' })
  setActivePinia(createPinia())
  try {
    const { threadApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
    t.mock.method(threadApi, 'getHistoryThreads', async (_agent, _limit, after, { isPinned } = {}) => {
      if (isPinned) return { data: [], has_more: false }
      return after
        ? { data: [{ id: 'older', yuxi: {} }], last_id: 'older', has_more: false }
        : { data: [{ id: 'first', yuxi: {} }], last_id: 'first', has_more: true }
    })
    t.mock.method(threadApi, 'createThread', async () => ({ id: 'created', yuxi: {} }))
    const { useChatThreadsStore } = await server.ssrLoadModule('/src/modules/session/model/chatThreads.js')
    const store = useChatThreadsStore()
    await store.loadThreads()
    store.upsertThread({ id: 'auto-root', yuxi: { parent_session_id: null } })
    store.setCurrentThreadId('auto-root')
    await store.loadThreads()
    assert.equal(store.currentThread.id, 'auto-root')
    assert.deepEqual(store.historyThreads.map(item => item.id), ['first'])
    await store.createThread('agent')
    await store.loadMoreThreads()
    assert.deepEqual(store.historyThreads.map(item => item.id), ['created', 'first', 'older'])
    const { useUserStore } = await server.ssrLoadModule('/src/modules/identity/model/user.js')
    useUserStore().userId = 'another-user'
    assert.deepEqual(store.historyThreads, [])
  } finally { await server.close() }
})
