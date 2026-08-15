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
