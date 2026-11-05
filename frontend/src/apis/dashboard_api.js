import { apiGet } from './base'

/**
 * Dashboard API模块
 * 用于超级管理员查看系统概览、调用监控与会话深度分析
 */

export const dashboardApi = {
  /**
   * 获取Dashboard基础统计信息
   * @returns {Promise<Object>} - 统计信息
   */
  getStats: () => {
    return apiGet('/api/dashboard/stats')
  },

  /** 读取执行节点当前运行与全部 VM 数量。 */
  getVmStats: () => apiGet('/api/dashboard/stats/vms'),

  /** 按创建范围读取智能体会话分布及平均消息数。 */
  getAgentSessionDistribution: (timeRange = 'all') =>
    apiGet(
      `/api/dashboard/stats/agents/sessions?${new URLSearchParams({ time_range: timeRange })}`
    ),

  /**
   * 批量获取系统概览所有统计数据（并行请求）
   * @returns {Promise<Object>} - 所有统计数据
   */
  getAllStats: async () => {
    try {
      const [basic, users, tools, knowledge] = await Promise.all([
        apiGet('/api/dashboard/stats'),
        apiGet('/api/dashboard/stats/users'),
        apiGet('/api/dashboard/stats/tools'),
        apiGet('/api/dashboard/stats/knowledge')
      ])
      return { basic, users, tools, knowledge }
    } catch (error) {
      console.error('批量获取统计数据失败:', error)
      throw error
    }
  },

  /**
   * 获取调用统计时间序列数据
   * @param {string} type - 数据类型 (models/agents/tokens/tools/sessions)
   * @param {string} timeRange - 时间范围 (14hours/14days/14weeks)
   * @returns {Promise<Object>} - 时间序列统计数据
   */
  getCallTimeseries: (type = 'models', timeRange = '14days') => {
    return apiGet(`/api/dashboard/stats/calls/timeseries?type=${type}&time_range=${timeRange}`)
  }
}
