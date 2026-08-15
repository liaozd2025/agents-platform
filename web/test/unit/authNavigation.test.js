import assert from 'node:assert/strict'
import test from 'node:test'

import { getAuthenticatedHomePath } from '../../src/utils/authNavigation.js'

test('登录后进入当前角色可访问的后台首页', () => {
  const dashboardOnly = (permission) => permission === 'dashboard:view'

  assert.equal(getAuthenticatedHomePath(dashboardOnly), '/dashboard')
  assert.equal(getAuthenticatedHomePath(() => false), '/settings/account')
})
