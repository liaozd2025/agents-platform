/**
 * 角色与权限只读总览 API。
 */

import { apiSuperAdminGet } from './base'

export const getRoleOverview = () => apiSuperAdminGet('/api/roles/overview')
