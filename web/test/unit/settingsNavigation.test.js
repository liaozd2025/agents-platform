import assert from 'node:assert/strict'
import test from 'node:test'

import { getSettingsNavigationGroups } from '../../src/utils/settingsNavigation.js'

test('设置导航按权限分组并支持搜索', () => {
  const superadminGroups = getSettingsNavigationGroups(
    {
      isLoggedIn: true,
      isAdmin: true,
      isSuperAdmin: true,
      effectivePermissions: [
        'role:read',
        'role:manage',
        'user:read',
        'system_config:manage',
        'ocr:manage'
      ]
    },
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
    { isLoggedIn: true, isAdmin: false, isSuperAdmin: false, effectivePermissions: [] },
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
    {
      isLoggedIn: true,
      isAdmin: true,
      isSuperAdmin: false,
      effectivePermissions: [
        'role:read',
        'user:read',
        'system_config:manage',
        'ocr:manage'
      ]
    },
    ''
  )
  assert.deepEqual(
    adminGroups.map((group) => [group.label, group.items.map((item) => item.id)]),
    [
      ['个人', ['account', 'agentEnv']],
      ['系统', ['base', 'user', 'role']],
      ['平台能力', ['apiKeys', 'ocr']]
    ]
  )

  const searchResult = getSettingsNavigationGroups(
    {
      isLoggedIn: true,
      isAdmin: true,
      isSuperAdmin: true,
      effectivePermissions: [
        'role:read',
        'role:manage',
        'user:read',
        'system_config:manage',
        'ocr:manage'
      ]
    },
    '组织'
  )
  assert.deepEqual(searchResult, [
    { label: '系统', items: [{ id: 'department', label: '组织机构' }] }
  ])

  assert.deepEqual(
    getSettingsNavigationGroups(
      {
        isLoggedIn: true,
        isAdmin: true,
        isSuperAdmin: true,
        effectivePermissions: [
          'role:read',
          'role:manage',
          'user:read',
          'system_config:manage',
          'ocr:manage'
        ]
      },
      '角色'
    ),
    [{ label: '系统', items: [{ id: 'role', label: '角色与权限' }] }]
  )

  assert.deepEqual(
    getSettingsNavigationGroups(
      { isLoggedIn: true, isAdmin: false, isSuperAdmin: false, effectivePermissions: [] },
      'env'
    ),
    [{ label: '个人', items: [{ id: 'agentEnv', label: '环境变量' }] }]
  )
})

test('角色入口只依赖服务端有效权限', () => {
  const groups = getSettingsNavigationGroups({
    isLoggedIn: true,
    isAdmin: false,
    isSuperAdmin: false,
    effectivePermissions: ['role:read']
  })

  assert.deepEqual(
    groups.map((group) => [group.label, group.items.map((item) => item.id)]),
    [
      ['个人', ['account', 'agentEnv']],
      ['系统', ['role']],
      ['平台能力', ['apiKeys']]
    ]
  )
})

test('用户管理入口只依赖服务端有效权限', () => {
  const groups = getSettingsNavigationGroups({
    isLoggedIn: true,
    isAdmin: false,
    isSuperAdmin: false,
    effectivePermissions: ['user:read']
  })

  assert.deepEqual(
    groups.map((group) => [group.label, group.items.map((item) => item.id)]),
    [
      ['个人', ['account', 'agentEnv']],
      ['系统', ['user']],
      ['平台能力', ['apiKeys']]
    ]
  )
})

test('平台设置入口只依赖对应功能权限', () => {
  const groups = getSettingsNavigationGroups({
    isLoggedIn: true,
    isAdmin: false,
    isSuperAdmin: false,
    effectivePermissions: ['system_config:manage', 'ocr:manage']
  })

  assert.deepEqual(
    groups.map((group) => [group.label, group.items.map((item) => item.id)]),
    [
      ['个人', ['account', 'agentEnv']],
      ['系统', ['base']],
      ['平台能力', ['apiKeys', 'ocr']]
    ]
  )
})
