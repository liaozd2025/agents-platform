import { apiGet, apiPost, apiPut, apiDelete } from './base'

const API_KEY_BASE_PATH = '/api/user/apikey'

/**
 * Public API 对外端点清单，供集成页展示；请求路径集中在 apis 边界维护。
 */
export const PUBLIC_API_ENDPOINTS = [
  { method: 'GET', path: '/api/v1/agents/', description: '列出可用智能体' },
  { method: 'POST', path: '/api/v1/agents/sessions', description: '创建会话' },
  { method: 'GET', path: '/api/v1/agents/sessions', description: '列出会话' },
  { method: 'POST', path: '/api/v1/agents/sessions/{session_id}/events', description: '提交消息或控制事件' },
  { method: 'GET', path: '/api/v1/agents/sessions/{session_id}/events', description: '订阅运行事件 (SSE)' },
  { method: 'GET', path: '/api/v1/knowledge/tools/list_kbs', description: '列出可见知识库' },
  { method: 'POST', path: '/api/v1/knowledge/tools/query_kb', description: '检索知识库内容' },
  { method: 'POST', path: '/api/v1/knowledge/tools/search_file', description: '搜索知识库文件' },
  { method: 'POST', path: '/api/v1/knowledge/tools/open_kb_document', description: '读取知识库文档' },
  { method: 'POST', path: '/api/v1/knowledge/tools/find_kb_document', description: '定位文档内容' }
]

export const apikeyApi = {
  list: () => apiGet(`${API_KEY_BASE_PATH}/`),

  listApps: () => apiGet(`${API_KEY_BASE_PATH}/apps`),

  create: (data) => apiPost(`${API_KEY_BASE_PATH}/`, data),

  get: (id) => apiGet(`${API_KEY_BASE_PATH}/${id}`),

  update: (id, data) => apiPut(`${API_KEY_BASE_PATH}/${id}`, data),

  delete: (id) => apiDelete(`${API_KEY_BASE_PATH}/${id}`)
}
