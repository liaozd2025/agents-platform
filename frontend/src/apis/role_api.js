/** 角色与权限管理 API。 */

import { apiGet, apiPost, apiPut } from './base'

export const getRoleOverview = (targetUserId = null) => {
  const query = targetUserId == null ? '' : `?target_user_id=${encodeURIComponent(targetUserId)}`
  return apiGet(`/api/roles/overview${query}`)
}

export const createRole = (data) => apiPost('/api/roles', data)

export const copyRole = (roleId, data) => apiPost(`/api/roles/${roleId}/copy`, data)

export const updateRole = (roleId, data) => apiPut(`/api/roles/${roleId}`, data)

export const deactivateRole = (roleId) => apiPost(`/api/roles/${roleId}/deactivate`)
