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

test('知识库评估详情同时要求评估管理与知识库访问权限', () => {
  const matched = [
    { meta: { requiredAnyPermissions: ['knowledge_base:read', 'knowledge_base:manage'] } },
    { meta: { requiredPermission: 'knowledge_evaluation:manage' } }
  ]

  assert.equal(
    canAccessRoute(matched, (permission) =>
      ['knowledge_base:read', 'knowledge_evaluation:manage'].includes(permission)
    ),
    true
  )
  assert.equal(
    canAccessRoute(matched, (permission) => permission === 'knowledge_base:read'),
    false
  )
  assert.equal(
    canAccessRoute(matched, (permission) => permission === 'knowledge_evaluation:manage'),
    false
  )
})
