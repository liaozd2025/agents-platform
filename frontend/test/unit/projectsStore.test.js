import assert from 'node:assert/strict'
import test from 'node:test'

import { createPinia, setActivePinia } from 'pinia'
import { createTestServer } from '../helpers/createTestServer.js'
import { buildProjectSessionGroups } from '../../src/modules/projects/model/projectSessionGroups.js'

globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {}
}

test('创建 Project 后迟到的列表响应不会覆盖侧边栏状态', async () => {
  const server = await createTestServer({ server: { middlewareMode: true }, appType: 'custom' })
  setActivePinia(createPinia())
  try {
    const { projectApi } = await server.ssrLoadModule('/src/apis/project_api.js')
    let resolveProjects
    projectApi.getProjects = () =>
      new Promise((resolve) => {
        resolveProjects = resolve
      })

    const { useProjectsStore } = await server.ssrLoadModule('/src/modules/projects/model/projects.js')
    const store = useProjectsStore()
    const loadPromise = store.loadProjects()
    const createdProject = {
      id: 'project-new',
      name: '新项目',
      selection_status: 'selectable',
      status: 'active'
    }

    store.upsertProject(createdProject)
    resolveProjects([])
    await loadPromise

    assert.deepEqual(store.projects, [createdProject])
    assert.equal(store.isLoading, false)
    const grouped = buildProjectSessionGroups(store.projects, [
      { id: 'thread-new', yuxi: { project_id: createdProject.id }, created_at: 1788256800 }
    ])
    assert.equal(grouped.groups[0].sessions[0].id, 'thread-new')
    assert.deepEqual(grouped.otherSessions, [])
  } finally {
    await server.close()
  }
})

test('项目刷新期间保留已加载状态，失败后仍可使用已有列表', async () => {
  const server = await createTestServer({ server: { middlewareMode: true }, appType: 'custom' })
  setActivePinia(createPinia())
  try {
    const { projectApi } = await server.ssrLoadModule('/src/apis/project_api.js')
    const { useProjectsStore } = await server.ssrLoadModule('/src/modules/projects/model/projects.js')
    const store = useProjectsStore()
    assert.equal(store.hasLoaded, false)
    projectApi.getProjects = async () => []
    await store.loadProjects()
    assert.equal(store.hasLoaded, true, '空列表也代表首次加载完成')
    const project = { id: 'retained', name: '保留的项目' }
    store.upsertProject(project)
    let rejectLoad
    projectApi.getProjects = () =>
      new Promise((_, reject) => {
        rejectLoad = reject
      })
    const refresh = store.loadProjects()
    assert.equal(store.isLoading, true)
    assert.equal(store.hasLoaded, true)
    assert.deepEqual(store.projects, [project])
    rejectLoad(new Error('offline'))
    await assert.rejects(refresh, /offline/)
    assert.equal(store.hasLoaded, true)
    assert.equal(store.isLoading, false)
    assert.equal(store.error, '项目加载失败')
    assert.deepEqual(store.projects, [project])
  } finally {
    await server.close()
  }
})

test('退出登录清空项目缓存，退出前的迟到响应不能跨会话写回', async () => {
  const server = await createTestServer({ server: { middlewareMode: true }, appType: 'custom' })
  setActivePinia(createPinia())
  try {
    const { projectApi } = await server.ssrLoadModule('/src/apis/project_api.js')
    const { useProjectsStore } = await server.ssrLoadModule('/src/modules/projects/model/projects.js')
    const { useUserStore } = await server.ssrLoadModule('/src/modules/identity/model/user.js')
    const store = useProjectsStore()
    projectApi.getProjects = async () => [{ id: 'account-a', name: '账号 A 的项目' }]
    await store.loadProjects()
    let resolveLoad
    projectApi.getProjects = () =>
      new Promise((resolve) => {
        resolveLoad = resolve
      })
    const pending = store.loadProjects()
    useUserStore().logout()
    assert.deepEqual(store.projects, [])
    assert.equal(store.hasLoaded, false)
    assert.equal(store.isLoading, false)
    resolveLoad([{ id: 'late-a' }])
    await pending
    assert.deepEqual(store.projects, [])
    assert.equal(store.hasLoaded, false)
    projectApi.getProjects = async () => {
      throw new Error('账号 B 加载失败')
    }
    await assert.rejects(store.loadProjects(), /账号 B/)
    assert.deepEqual(store.projects, [])
    assert.equal(store.hasLoaded, false)
  } finally {
    await server.close()
  }
})

test('OA 账号切换清空旧 Project 并允许新账号重新加载', async () => {
  const server = await createTestServer({ server: { middlewareMode: true }, appType: 'custom' })
  setActivePinia(createPinia())
  try {
    const { projectApi } = await server.ssrLoadModule('/src/apis/project_api.js')
    let resolveOldProjects
    projectApi.getProjects = () =>
      new Promise((resolve) => {
        resolveOldProjects = resolve
      })

    const { useProjectsStore } = await server.ssrLoadModule('/src/modules/projects/model/projects.js')
    const store = useProjectsStore()
    const oldLoad = store.loadProjects()
    store.reset()
    resolveOldProjects([{ id: 'project-account-a' }])
    await oldLoad

    assert.deepEqual(store.projects, [])

    projectApi.getProjects = async () => [{ id: 'project-account-b' }]
    await store.loadProjects()
    assert.deepEqual(store.projects, [{ id: 'project-account-b' }])
  } finally {
    await server.close()
  }
})


test('账号切换后旧目录名称请求不能写回新账号缓存', async () => {
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  const server = await createTestServer({ server: { middlewareMode: true }, appType: 'custom' })
  try {
    setActivePinia(createPinia())
    const { useProjectsStore } = await server.ssrLoadModule('/src/modules/projects/model/projects.js')
    const { projectApi } = await server.ssrLoadModule('/src/apis/project_api.js')
    let finish
    projectApi.getProjects = () => new Promise((resolve) => { finish = resolve })
    projectApi.getHistoryCandidates = async () => ({ items: [] })
    const store = useProjectsStore()
    const pending = store.ensureDirectoryLabels()
    store.reset()
    finish([{ id: '548f7838-d284-4d5e-b977-188f55cca0d4', name: '旧账号私有名称', workdir_path: 'projects/548f7838-d284-4d5e-b977-188f55cca0d4' }])
    await pending
    assert.deepEqual(store.directoryLabels, {})
  } finally { await server.close() }
})
