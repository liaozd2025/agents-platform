import assert from 'node:assert/strict'
import test from 'node:test'

import { canAccessRoute, getAuthenticatedHomePath } from '../../src/utils/authNavigation.js'

test('登录后进入当前角色可访问的后台首页', () => {
  const dashboardOnly = (permission) => permission === 'dashboard:view'

  assert.equal(getAuthenticatedHomePath(dashboardOnly), '/dashboard')
  assert.equal(
    getAuthenticatedHomePath(() => false),
    '/settings/account'
  )
})

test('路由权限同时支持全部满足与任一满足约束', () => {
  const matched = [
    { meta: { requiredPermission: 'dashboard:view' } },
    { meta: { requiredAnyPermissions: ['user:read', 'role:read'] } }
  ]

  assert.equal(
    canAccessRoute(matched, (permission) => ['dashboard:view', 'role:read'].includes(permission)),
    true
  )
  assert.equal(
    canAccessRoute(matched, (permission) => permission === 'dashboard:view'),
    false
  )
})
