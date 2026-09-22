import assert from 'node:assert/strict'
import test from 'node:test'

import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'

const storageValues = new Map()
globalThis.localStorage = {
  getItem: (key) => storageValues.get(key) ?? null,
  setItem: (key, value) => storageValues.set(key, String(value)),
  removeItem: (key) => storageValues.delete(key),
  clear: () => storageValues.clear()
}

async function withServer(run) {
  const server = await createServer({
    server: { middlewareMode: true },
    appType: 'custom',
    ssr: { noExternal: ['ant-design-vue'] },
    plugins: [
      {
        name: 'test-message-api',
        enforce: 'pre',
        resolveId(id) {
          return id === 'ant-design-vue' ? '\0test-message-api' : null
        },
        load(id) {
          if (id !== '\0test-message-api') return null
          return `export const message = {
            error(value) { globalThis.__apiBoundaryMessages.push(value) }
          }`
        }
      }
    ]
  })

  try {
    await run(server)
  } finally {
    await server.close()
  }
}

test('知识库图片仅向同源图片接口发送认证头', async () => {
  await withServer(async (server) => {
    storageValues.set('user_token', 'synthetic-image-token')
    globalThis.window = { location: new URL('https://app.example/agent') }
    setActivePinia(createPinia())
    const calls = []
    const originalFetch = globalThis.fetch
    globalThis.fetch = async (url, options) => {
      calls.push({ url, options })
      return new Response('image-bytes', { headers: { 'content-type': 'image/png' } })
    }
    try {
      const { fetchKnowledgeImage } = await server.ssrLoadModule('/src/apis/knowledge_api.js')
      for (const src of [
        'https://outside.example/api/knowledge/databases/a/images/x',
        '//outside.example/api/knowledge/databases/a/images/x',
        'http://app.example/api/knowledge/databases/a/images/x',
        'https://app.example:444/api/knowledge/databases/a/images/x',
        '/capture?looks=/api/knowledge/databases/a/images/x',
        '/prefix/api/knowledge/databases/a/images/x',
        '/api/knowledge/databases/a/images/../../../../capture',
        'data:image/png;base64,AA==',
        'http://['
      ]) {
        assert.equal(await fetchKnowledgeImage(src), null, src)
      }
      assert.equal(calls.length, 0)
      for (const src of [
        '/api/knowledge/databases/a/images/x',
        'https://app.example/api/knowledge/databases/a/images/x',
        '//app.example/api/knowledge/databases/a/images/x'
      ]) {
        const blob = await fetchKnowledgeImage(src)
        assert.ok(blob instanceof Blob)
        assert.equal(await blob.text(), 'image-bytes')
      }
      assert.equal(calls.length, 3)
      for (const { url, options } of calls) {
        assert.equal(url, 'https://app.example/api/knowledge/databases/a/images/x')
        assert.equal(options.headers.Authorization, 'Bearer synthetic-image-token')
        assert.equal(options.mode, 'same-origin')
      }
    } finally {
      globalThis.fetch = originalFetch
    }
  })
})

test('公开登录 401 保留服务端错误且不清理当前会话', async () => {
  await withServer(async (server) => {
    storageValues.set('user_token', 'existing-token')
    globalThis.__apiBoundaryMessages = []
    globalThis.window = { location: { href: '/current' } }
    globalThis.fetch = async () =>
      new Response(JSON.stringify({ detail: '用户名或密码错误' }), {
        status: 401,
        headers: { 'content-type': 'application/json' }
      })

    const { authApi } = await server.ssrLoadModule('/src/apis/auth_api.js')

    await assert.rejects(
      authApi.login({ loginId: 'someone', password: 'wrong' }),
      (error) => error.message === '用户名或密码错误' && error.status === 401
    )
    assert.equal(storageValues.get('user_token'), 'existing-token')
    assert.equal(window.location.href, '/current')
    assert.deepEqual(globalThis.__apiBoundaryMessages, [])
  })
})

test('登录 423 保留锁定状态、文案和剩余时间响应头', async () => {
  await withServer(async (server) => {
    globalThis.fetch = async () =>
      new Response(JSON.stringify({ detail: '账户已锁定 60 秒' }), {
        status: 423,
        headers: {
          'content-type': 'application/json',
          'X-Lock-Remaining': '60'
        }
      })

    const { authApi } = await server.ssrLoadModule('/src/apis/auth_api.js')

    await assert.rejects(authApi.login({ loginId: 'someone', password: 'wrong' }), (error) => {
      assert.equal(error.message, '账户已锁定 60 秒')
      assert.equal(error.status, 423)
      assert.equal(error.headers.get('X-Lock-Remaining'), '60')
      return true
    })
  })
})

test('受保护请求的 401 才清理会话并跳转登录页', async () => {
  await withServer(async (server) => {
    storageValues.set('user_token', 'expired-token')
    globalThis.__apiBoundaryMessages = []
    globalThis.window = { location: { href: '/agent' } }
    globalThis.fetch = async () =>
      new Response(JSON.stringify({ detail: '令牌已过期' }), {
        status: 401,
        headers: { 'content-type': 'application/json' }
      })

    const originalSetTimeout = globalThis.setTimeout
    try {
      setActivePinia(createPinia())
      const { apiGet } = await server.ssrLoadModule('/src/apis/base.js')
      const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
      const userStore = useUserStore()
      globalThis.setTimeout = (callback) => {
        callback()
        return 0
      }

      await assert.rejects(apiGet('/api/protected'), (error) => error.status === 401)
      assert.equal(userStore.isLoggedIn, false)
      assert.equal(storageValues.has('user_token'), false)
      assert.equal(window.location.href, '/login')
      assert.deepEqual(globalThis.__apiBoundaryMessages, ['登录已过期，请重新登录'])
    } finally {
      globalThis.setTimeout = originalSetTimeout
    }
  })
})

test('用户列表只发起一次分页请求并读取总数响应头', async () => {
  await withServer(async (server) => {
    storageValues.clear()
    const requestedUrls = []
    globalThis.fetch = async (url) => {
      requestedUrls.push(String(url))
      return new Response(JSON.stringify([{ id: 1, username: 'page-user' }]), {
        status: 200,
        headers: {
          'content-type': 'application/json',
          'X-Total-Count': '27'
        }
      })
    }

    setActivePinia(createPinia())
    const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
    const userStore = useUserStore()
    userStore.token = 'test-token'

    const result = await userStore.getUsers({
      skip: 10,
      limit: 10,
      keyword: 'page',
      departmentId: 8,
      role: 'user'
    })

    assert.deepEqual(result, { users: [{ id: 1, username: 'page-user' }], total: 27 })
    assert.deepEqual(requestedUrls, [
      '/api/auth/users?skip=10&limit=10&keyword=page&department_id=8&role=user'
    ])
  })
})

test('用户 Store 的 422 传播链不泄露认证头、密码或 Pydantic input', async () => {
  await withServer(async (server) => {
    storageValues.clear()
    const secretToken = 'secret-bearer-token'
    const secretPassword = 'secret-password'
    const secretResponse = 'secret-response-context'
    const logged = []
    const originalConsoleError = console.error

    try {
      console.error = (...values) => logged.push(values)
      globalThis.fetch = async () =>
        new Response(
          JSON.stringify({
            detail: [
              {
                loc: ['body', 'password'],
                msg: 'Value error',
                type: 'value_error',
                input: secretPassword,
                ctx: { secret: secretResponse }
              }
            ]
          }),
          {
            status: 422,
            statusText: 'Unprocessable Entity',
            headers: { 'content-type': 'application/json' }
          }
        )

      setActivePinia(createPinia())
      const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
      const userStore = useUserStore()
      userStore.token = secretToken
      userStore.userId = 1

      await assert.rejects(
        userStore.createUser({ username: 'new-user', password: secretPassword }),
        (error) => error.status === 422
      )

      const serializedLogs = JSON.stringify(logged)
      assert.equal(serializedLogs.includes(secretToken), false)
      assert.equal(serializedLogs.includes(secretPassword), false)
      assert.equal(serializedLogs.includes(secretResponse), false)
      assert.equal(serializedLogs.includes('/api/auth/users'), true)
      assert.equal(serializedLogs.includes('422'), true)
    } finally {
      console.error = originalConsoleError
    }
  })
})

test('用户 Store 的普通错误传播链不附着或记录服务端任意响应上下文', async () => {
  await withServer(async (server) => {
    storageValues.clear()
    const secretToken = 'secret-bearer-token'
    const secretPassword = 'secret-password'
    const secretResponse = 'secret-response-context'
    const logged = []
    const originalConsoleError = console.error

    try {
      console.error = (...values) => logged.push(values)
      globalThis.fetch = async () =>
        new Response(
          JSON.stringify({
            detail: {
              message: secretResponse,
              context: { note: secretPassword }
            }
          }),
          {
            status: 400,
            statusText: secretResponse,
            headers: { 'content-type': 'application/json' }
          }
        )

      setActivePinia(createPinia())
      const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
      const userStore = useUserStore()
      userStore.token = secretToken
      userStore.userId = 1

      await assert.rejects(
        userStore.createUser({ username: 'new-user', password: secretPassword }),
        (error) => {
          assert.equal(error.status, 400)
          assert.equal(error.message, '请求参数错误')
          assert.deepEqual(error.response.data, { detail: '请求参数错误' })
          return true
        }
      )

      const serializedLogs = JSON.stringify(logged)
      assert.equal(serializedLogs.includes(secretToken), false)
      assert.equal(serializedLogs.includes(secretPassword), false)
      assert.equal(serializedLogs.includes(secretResponse), false)
      assert.equal(serializedLogs.includes('/api/auth/users'), true)
      assert.equal(serializedLogs.includes('400'), true)
    } finally {
      console.error = originalConsoleError
    }
  })
})

test('用户管理分页 API 只请求当前页并编码服务端筛选条件', async () => {
  await withServer(async (server) => {
    storageValues.set('user_token', 'test-token')
    const requests = []
    globalThis.fetch = async (url) => {
      requests.push(String(url))
      return new Response(JSON.stringify({ items: [], total: 0, limit: 20, offset: 40 }), {
        status: 200,
        headers: { 'content-type': 'application/json' }
      })
    }

    setActivePinia(createPinia())
    const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
    useUserStore().userRole = 'superadmin'
    const { authApi } = await server.ssrLoadModule('/src/apis/auth_api.js')

    const page = await authApi.getUsersPage({
      offset: 40,
      limit: 20,
      search: '张 三',
      departmentId: 3,
      role: 'admin'
    })

    assert.deepEqual(requests, [
      '/api/auth/users/page?offset=40&limit=20&search=%E5%BC%A0+%E4%B8%89&department_id=3&role=admin'
    ])
    assert.equal(page.total, 0)
  })
})

test('用户管理组件不再通过 Store 全量加载用户', async () => {
  const source = await import('node:fs/promises').then((fs) =>
    fs.readFile(
      new URL('../../src/components/UserManagementComponent.vue', import.meta.url),
      'utf8'
    )
  )

  assert.equal(source.includes('authApi.getUsersPage('), true)
  assert.equal(source.includes('userStore.getUsers()'), false)
  assert.equal(source.includes('filteredUsers'), false)
  assert.equal(source.includes('paginatedUsers'), false)
})

test('知识库 API 单一构造并编码文件上传端点', async () => {
  await withServer(async (server) => {
    const { fileApi } = await server.ssrLoadModule('/src/apis/knowledge_api.js')

    assert.equal(fileApi.getUploadUrl(), '/api/knowledge/files/upload')
    assert.equal(
      fileApi.getUploadUrl('kb/with space'),
      '/api/knowledge/files/upload?kb_id=kb%2Fwith%20space'
    )
  })
})

test('共享查询参数边界保留 false/0 并省略 undefined/null/空字符串', async () => {
  await withServer(async (server) => {
    const { buildQuery } = await server.ssrLoadModule('/src/apis/base.js')

    assert.equal(
      buildQuery({
        false_value: false,
        zero_value: 0,
        empty_value: '',
        null_value: null,
        undefined_value: undefined,
        text: 'a b/c'
      }),
      'false_value=false&zero_value=0&text=a+b%2Fc'
    )
  })
})

test('四个 API 模块复用查询参数边界并保持 endpoint 问号语义', async () => {
  await withServer(async (server) => {
    storageValues.set('user_token', 'test-token')
    const requests = []
    globalThis.fetch = async (url) => {
      requests.push(String(url))
      return new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { 'content-type': 'application/json' }
      })
    }

    setActivePinia(createPinia())
    const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
    useUserStore().userRole = 'admin'
    const { projectApi } = await server.ssrLoadModule('/src/apis/project_api.js')
    const { searchViewerFiles } = await server.ssrLoadModule('/src/apis/viewer_filesystem.js')
    const { getWorkspaceKnowledgeTree } = await server.ssrLoadModule('/src/apis/workspace_api.js')
    const { documentApi } = await server.ssrLoadModule('/src/apis/knowledge_api.js')

    await projectApi.getHistoryCandidates({ query: '', limit: 0, offset: 0 })
    await searchViewerFiles('thread-1', '')
    await getWorkspaceKnowledgeTree('kb-1', {
      page: 0,
      pageSize: false,
      recursive: false,
      filesOnly: false
    })
    await documentApi.listDocuments('kb-1', {
      page: 0,
      page_size: false,
      empty: '',
      ignored: null
    })
    await documentApi.listDocuments('kb-1')
    await documentApi.documentExists('kb-1')

    assert.deepEqual(requests, [
      '/api/projects/history-candidates?limit=0&offset=0',
      '/api/viewer/filesystem/search?thread_id=thread-1',
      '/api/workspace/knowledge/tree?kb_id=kb-1&page=0&page_size=false&recursive=false&files_only=false',
      '/api/knowledge/databases/kb-1/documents?page=0&page_size=false',
      '/api/knowledge/databases/kb-1/documents',
      '/api/knowledge/databases/kb-1/documents/exists?'
    ])
  })
})

test('Project 与 Workspace API 按 xhome 契约构造请求', async () => {
  await withServer(async (server) => {
    storageValues.set('user_token', 'test-token')
    const requests = []
    globalThis.fetch = async (url, options = {}) => {
      requests.push({ url, options })
      return new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { 'content-type': 'application/json' }
      })
    }

    setActivePinia(createPinia())
    const { projectApi } = await server.ssrLoadModule('/src/apis/project_api.js')
    const { createWorkspaceDirectory, getWorkspaceTree } = await server.ssrLoadModule(
      '/src/apis/workspace_api.js'
    )

    await projectApi.getProjects()
    await projectApi.createProject({
      requestId: 'request-1',
      name: '客户交付',
      mode: 'linked',
      path: '/clients/acme'
    })
    await projectApi.getHistoryCandidates({ query: '交付', limit: 10, offset: 20 })
    await getWorkspaceTree('/projects')
    await getWorkspaceTree('/projects', false, false, true)
    await createWorkspaceDirectory('/projects', '客户交付')

    assert.equal(requests[0].url, '/api/projects')
    assert.deepEqual(JSON.parse(requests[1].options.body), {
      request_id: 'request-1',
      name: '客户交付',
      workdir: { mode: 'linked', path: 'clients/acme' }
    })
    assert.equal(
      requests[2].url,
      '/api/projects/history-candidates?q=%E4%BA%A4%E4%BB%98&limit=10&offset=20'
    )
    assert.equal(
      requests[3].url,
      '/api/workspace/tree?path=%2Fprojects&recursive=false&files_only=false'
    )
    assert.equal(
      requests[4].url,
      '/api/workspace/tree?path=%2Fprojects&recursive=false&files_only=false&include_unbound_project_dirs=true'
    )
    assert.deepEqual(JSON.parse(requests[5].options.body), {
      parent_path: '/projects',
      name: '客户交付'
    })
  })
})

test('工具元数据 API 使用普通用户认证且普通用户可正常请求', async () => {
  await withServer(async (server) => {
    storageValues.clear()
    storageValues.set('user_token', 'user-token')
    globalThis.fetch = async (url) => {
      assert.equal(url, '/api/system/tools')
      return new Response(
        JSON.stringify({ success: true, data: [{ name: '搜索', slug: 'web_search' }] }),
        {
          status: 200,
          headers: { 'content-type': 'application/json' }
        }
      )
    }

    setActivePinia(createPinia())
    const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
    const userStore = useUserStore()
    userStore.token = 'user-token'
    userStore.role = 'user' // 非管理员

    const { toolApi } = await server.ssrLoadModule('/src/apis/tool_api.js')
    const result = await toolApi.getTools()
    assert.equal(result.success, true)
    assert.equal(result.data.length, 1)
    assert.equal(result.data[0].slug, 'web_search')
  })
})

test('创建 Run 丢响应后仅以原编号和原请求体重放', async () => {
  await withServer(async (server) => {
    globalThis.window = { location: { href: '/current' } }
    const calls = []
    globalThis.fetch = async (url, options) => {
      calls.push({ url, body: JSON.parse(options.body) })
      if (calls.length === 1) throw new TypeError('synthetic response lost after commit')
      return new Response(JSON.stringify({ request_id: 'stable-request', run_id: 'same-run' }), {
        headers: { 'content-type': 'application/json' }
      })
    }
    const { agentApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
    const result = await agentApi.createAgentRun({
      query: 'once',
      thread_id: 'same-thread',
      agent_slug: 'agent',
      meta: { request_id: 'stable-request', attachment_file_ids: ['file'] },
      model_spec: 'provider:model'
    })
    assert.equal(result.run_id, 'same-run')
    assert.equal(calls.length, 2)
    assert.deepEqual(calls[0], calls[1])
  })
})

test('提交被明确拒绝或缺少幂等编号时不自动重放，重放丢响应保留未知结果', async () => {
  await withServer(async (server) => {
    const { agentApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
    for (const [meta, status, count, uncertain] of [
      [{ request_id: 'stable' }, 400, 1, false],
      [{}, 0, 1, false],
      [{ request_id: 'stable' }, 0, 2, true]
    ]) {
      let calls = 0
      globalThis.fetch = async () => {
        calls++
        if (!status) throw new TypeError('connection lost')
        return new Response(JSON.stringify({ detail: 'rejected' }), {
          status,
          headers: { 'content-type': 'application/json' }
        })
      }
      await assert.rejects(agentApi.createAgentRun({ query: 'once', meta }), (error) => {
        assert.equal(Boolean(error.submissionUncertain), uncertain)
        return true
      })
      assert.equal(calls, count)
    }
  })
})
