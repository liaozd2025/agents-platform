import { apiGet } from './base'

const withDepartment = (path, departmentId) => {
  if (departmentId == null) return path
  return `${path}${path.includes('?') ? '&' : '?'}department_id=${departmentId}`
}

/**
 * Dashboard API模块
 * 用于有 dashboard:view 权限的用户查看授权组织范围内的数据
 */

export const dashboardApi = {
  getCurrentOrganizationStats: (departmentId = null) => {
    const query = departmentId == null ? '' : `?department_id=${departmentId}`
    return apiGet(`/api/dashboard/stats/current-organization${query}`)
  },

  /**
   * 获取所有对话记录
   * @param {Object} params - 查询参数
   * @param {string} [params.uid] - 用户 UID 过滤
   * @param {string} [params.agent_id] - 智能体ID过滤
   * @param {string} [params.status] - 状态过滤 (active/archived/deleted/all)
   * @param {string} [params.search] - 标题/ID/UID 关键字搜索
   * @param {number} [params.limit] - 每页数量
   * @param {number} [params.offset] - 偏移量
   * @returns {Promise<Object>} - 分页对话列表（items/total/limit/offset）
   */
  getConversations: (params = {}) => {
    const queryParams = new URLSearchParams()
    if (params.uid) queryParams.append('uid', params.uid)
    if (params.agent_id) queryParams.append('agent_id', params.agent_id)
    if (params.status) queryParams.append('status', params.status)
    if (params.search) queryParams.append('search', params.search)
    if (params.limit) queryParams.append('limit', params.limit)
    if (params.offset) queryParams.append('offset', params.offset)
    if (params.department_id != null) queryParams.append('department_id', params.department_id)

    return apiGet(`/api/dashboard/conversations?${queryParams.toString()}`)
  },

  /**
   * 获取会话审计筛选选项
   * @returns {Promise<Object>} - 用户与智能体选项
   */
  getConversationFilterOptions: (departmentId = null) => {
    return apiGet(withDepartment('/api/dashboard/conversations/options', departmentId))
  },

  /**
   * 获取对话详情
   * @param {string} threadId - 对话线程ID
   * @returns {Promise<Object>} - 对话详情
   */
  getConversationDetail: (threadId, departmentId = null) => {
    return apiGet(withDepartment(`/api/dashboard/conversations/${threadId}`, departmentId))
  },

  /**
   * 获取Dashboard基础统计信息
   * @returns {Promise<Object>} - 统计信息
   */
  getStats: (departmentId = null) => {
    return apiGet(withDepartment('/api/dashboard/stats', departmentId))
  },

  /**
   * 获取用户反馈列表
   * @param {Object} params - 查询参数
   * @param {string} [params.rating] - 反馈类型过滤 (like/dislike/all)
   * @param {string} [params.agent_id] - 智能体ID过滤
   * @returns {Promise<Array>} - 反馈列表
   */
  getFeedbacks: (params = {}) => {
    const queryParams = new URLSearchParams()
    if (params.rating && params.rating !== 'all') queryParams.append('rating', params.rating)
    if (params.agent_id) queryParams.append('agent_id', params.agent_id)
    if (params.department_id != null) queryParams.append('department_id', params.department_id)

    return apiGet(`/api/dashboard/feedbacks?${queryParams.toString()}`)
  },

  /**
   * 获取用户活跃度统计
   * @returns {Promise<Object>} - 用户活跃度统计信息
   */
  getUserStats: (departmentId = null) => {
    return apiGet(withDepartment('/api/dashboard/stats/users', departmentId))
  },

  /**
   * 获取工具调用统计
   * @returns {Promise<Object>} - 工具调用统计信息
   */
  getToolStats: (departmentId = null) => {
    return apiGet(withDepartment('/api/dashboard/stats/tools', departmentId))
  },

  /**
   * 获取知识库统计
   * @returns {Promise<Object>} - 知识库统计信息
   */
  getKnowledgeStats: (departmentId = null) => {
    return apiGet(withDepartment('/api/dashboard/stats/knowledge', departmentId))
  },

  getResourceStats: (departmentId = null) => {
    return apiGet(withDepartment('/api/dashboard/stats/resources', departmentId))
  },

  /**
   * 获取AI智能体分析数据
   * @returns {Promise<Object>} - AI智能体分析信息
   */
  getAgentStats: (departmentId = null) => {
    return apiGet(withDepartment('/api/dashboard/stats/agents', departmentId))
  },

  /**
   * 获取会话（Thread）多维分析统计
   * @param {Object} params - 查询参数
   * @param {string} [params.timeRange='30days'] - 时间范围 (7days/14days/30days/90days)
   * @param {string} [params.agentId] - 智能体过滤
   * @param {boolean} [params.includeSubagents=false] - 是否纳入子智能体会话
   * @returns {Promise<Object>} - 会话分析统计数据
   */
  getThreadStats: (params = {}) => {
    const queryParams = new URLSearchParams()
    if (params.timeRange) queryParams.append('time_range', params.timeRange)
    if (params.agentId) queryParams.append('agent_id', params.agentId)
    if (params.includeSubagents) queryParams.append('include_subagents', 'true')
    if (params.departmentId != null) queryParams.append('department_id', params.departmentId)

    return apiGet(`/api/dashboard/stats/threads?${queryParams.toString()}`)
  },

  /**
   * 批量获取所有统计数据（并行请求）
   * @param {Object} options - 当前运行时能力与组织范围
   * @param {boolean} options.includeKnowledge - 是否请求知识库统计
   * @param {boolean} options.includeResources - 是否请求资源统计
   * @param {number|null} options.departmentId - 部门范围
   * @returns {Promise<Object>} - 所有统计数据
   */
  getAllStats: async (
    { includeKnowledge = false, includeResources = false, departmentId = null } = {}
  ) => {
    try {
      const requests = {
        basic: apiGet(withDepartment('/api/dashboard/stats', departmentId)),
        users: apiGet(withDepartment('/api/dashboard/stats/users', departmentId)),
        tools: apiGet(withDepartment('/api/dashboard/stats/tools', departmentId)),
        agents: apiGet(withDepartment('/api/dashboard/stats/agents', departmentId))
      }
      if (includeKnowledge) {
        requests.knowledge = apiGet(withDepartment('/api/dashboard/stats/knowledge', departmentId))
      }
      if (includeResources) {
        requests.resources = apiGet(withDepartment('/api/dashboard/stats/resources', departmentId))
      }

      const entries = Object.entries(requests)
      const values = await Promise.all(entries.map(([, request]) => request))
      return {
        knowledge: null,
        resources: null,
        ...Object.fromEntries(entries.map(([name], index) => [name, values[index]]))
      }
    } catch (error) {
      console.error('批量获取统计数据失败:', error)
      throw error
    }
  },

  /**
   * 获取调用统计时间序列数据
   * @param {string} type - 数据类型 (models/agents/tokens/tools)
   * @param {string} timeRange - 时间范围 (14hours/14days/14weeks)
   * @returns {Promise<Object>} - 时间序列统计数据
   */
  getCallTimeseries: (type = 'models', timeRange = '14days', departmentId = null) => {
    return apiGet(
      withDepartment(
        `/api/dashboard/stats/calls/timeseries?type=${type}&time_range=${timeRange}`,
        departmentId
      )
    )
  }
}
