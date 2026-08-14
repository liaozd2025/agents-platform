/** 角色与权限管理 API。 */

import { apiSuperAdminGet, apiSuperAdminPost, apiSuperAdminPut } from './base'

export const getRoleOverview = () => apiSuperAdminGet('/api/roles/overview')

export const createRole = (data) => apiSuperAdminPost('/api/roles', data)

export const copyRole = (roleId, data) => apiSuperAdminPost(`/api/roles/${roleId}/copy`, data)

export const updateRole = (roleId, data) => apiSuperAdminPut(`/api/roles/${roleId}`, data)

export const deactivateRole = (roleId) => apiSuperAdminPost(`/api/roles/${roleId}/deactivate`)
