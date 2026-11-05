import { apiDelete, apiGet, apiPost } from './base'

const BASE_URL = '/api/background-jobs'

export const backgroundJobsApi = {
  fetchJobs: async (params = {}) => {
    const query = new URLSearchParams(params).toString()
    const url = query ? `${BASE_URL}?${query}` : BASE_URL
    return apiGet(url)
  },

  fetchJobDetail: async (jobId) => {
    return apiGet(`${BASE_URL}/${jobId}`)
  },

  cancelJob: async (jobId) => {
    return apiPost(`${BASE_URL}/${jobId}/cancel`, {})
  },

  deleteJob: async (jobId) => {
    return apiDelete(`${BASE_URL}/${jobId}`)
  }
}
