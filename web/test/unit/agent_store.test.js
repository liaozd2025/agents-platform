import assert from 'node:assert/strict'
import test from 'node:test'

import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'

globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {}
}

test('聊天初始化只请求当前用户可访问的辅助资源', async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })

  try {
    setActivePinia(createPinia())
    const { agentApi, databaseApi, mcpApi, skillApi, toolApi } =
      await server.ssrLoadModule('/src/apis/index.js')
    const { useAgentStore } = await server.ssrLoadModule('/src/stores/agent.js')
    const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
    const requests = []

    agentApi.getAgents = async () => (requests.push('agent'), { agents: [] })
    databaseApi.getAccessibleDatabases = async () => (requests.push('knowledge'), { databases: [] })
    mcpApi.getMcpServers = async () => (requests.push('mcp'), { data: [] })
    skillApi.listAccessibleSkills = async () => (requests.push('skill'), { data: [] })
    toolApi.getTools = async () => (requests.push('tool'), { data: [] })

    useUserStore().effectivePermissions = ['agent:use']
    await useAgentStore().initialize()

    assert.deepEqual(requests, ['agent'])
  } finally {
    await server.close()
  }
})

test('强制刷新 Agent 详情会更新缓存中的 Skill 选项', async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })

  try {
    setActivePinia(createPinia())
    const { agentApi } = await server.ssrLoadModule('/src/apis/index.js')
    const { useAgentStore } = await server.ssrLoadModule('/src/stores/agent.js')
    let requestCount = 0

    agentApi.getAgentDetail = async () => {
      requestCount += 1
      return {
        agent: {
          id: 'agent-1',
          configurable_items: {
            skills: {
              kind: 'skills',
              options: [{ value: requestCount === 1 ? 'old-skill' : 'new-skill' }]
            }
          }
        }
      }
    }

    const store = useAgentStore()
    await store.fetchAgentDetail('agent-1')
    await store.fetchAgentDetail('agent-1')
    assert.equal(requestCount, 1)
    assert.equal(store.agentDetails['agent-1'].configurable_items.skills.options[0].value, 'old-skill')

    // 安装 Skill 后必须绕过详情缓存，才能让对话页拿到新选项。
    await store.fetchAgentDetail('agent-1', true)
    assert.equal(requestCount, 2)
    assert.equal(store.agentDetails['agent-1'].configurable_items.skills.options[0].value, 'new-skill')
  } finally {
    await server.close()
  }
})
