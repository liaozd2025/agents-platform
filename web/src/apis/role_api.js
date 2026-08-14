/** 角色与权限管理 API。 */

import { apiGet, apiSuperAdminPost, apiSuperAdminPut } from './base'

export const getRoleOverview = (targetUserId = null) => {
  const query = targetUserId == null ? '' : `?target_user_id=${encodeURIComponent(targetUserId)}`
  return apiGet(`/api/roles/overview${query}`)
}

export const createRole = (data) => apiSuperAdminPost('/api/roles', data)

export const copyRole = (roleId, data) => apiSuperAdminPost(`/api/roles/${roleId}/copy`, data)

export const updateRole = (roleId, data) => apiSuperAdminPut(`/api/roles/${roleId}`, data)

export const deactivateRole = (roleId) => apiSuperAdminPost(`/api/roles/${roleId}/deactivate`)
