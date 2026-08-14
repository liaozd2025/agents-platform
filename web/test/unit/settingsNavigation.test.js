import assert from 'node:assert/strict'
import test from 'node:test'

import { getSettingsNavigationGroups } from '../../src/utils/settingsNavigation.js'

test('设置导航按权限分组并支持搜索', () => {
  const superadminGroups = getSettingsNavigationGroups(
    { isLoggedIn: true, isAdmin: true, isSuperAdmin: true },
    ''
  )
  assert.deepEqual(
    superadminGroups.map((group) => [group.label, group.items.map((item) => item.id)]),
    [
      ['个人', ['account', 'agentEnv']],
      ['系统', ['base', 'user', 'department', 'role']],
      ['平台能力', ['apiKeys', 'ocr']]
    ]
  )

  const userGroups = getSettingsNavigationGroups(
    { isLoggedIn: true, isAdmin: false, isSuperAdmin: false },
    ''
  )
  assert.deepEqual(
    userGroups.map((group) => [group.label, group.items.map((item) => item.id)]),
    [
      ['个人', ['account', 'agentEnv']],
      ['平台能力', ['apiKeys']]
    ]
  )

  const adminGroups = getSettingsNavigationGroups(
    { isLoggedIn: true, isAdmin: true, isSuperAdmin: false },
    ''
  )
  assert.deepEqual(
    adminGroups.map((group) => [group.label, group.items.map((item) => item.id)]),
    [
      ['个人', ['account', 'agentEnv']],
      ['系统', ['base', 'user']],
      ['平台能力', ['apiKeys', 'ocr']]
    ]
  )

  const searchResult = getSettingsNavigationGroups(
    { isLoggedIn: true, isAdmin: true, isSuperAdmin: true },
    '组织'
  )
  assert.deepEqual(searchResult, [
    { label: '系统', items: [{ id: 'department', label: '组织机构' }] }
  ])

  assert.deepEqual(
    getSettingsNavigationGroups(
      { isLoggedIn: true, isAdmin: true, isSuperAdmin: true },
      '角色'
    ),
    [{ label: '系统', items: [{ id: 'role', label: '角色与权限' }] }]
  )

  assert.deepEqual(
    getSettingsNavigationGroups({ isLoggedIn: true, isAdmin: false, isSuperAdmin: false }, 'env'),
    [{ label: '个人', items: [{ id: 'agentEnv', label: '环境变量' }] }]
  )
})
