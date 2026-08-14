import assert from 'node:assert/strict'
import test from 'node:test'

import { getDataScopeLabel, groupRolePermissions } from '../../src/utils/roleOverview.js'

test('角色权限按服务端目录分组并忽略未选权限', () => {
  const catalog = [
    { key: 'user:read', name: '查看用户', group: '用户与组织' },
    { key: 'user:create', name: '创建用户', group: '用户与组织' },
    { key: 'dashboard:view', name: '查看 Dashboard', group: '统计分析' }
  ]

  assert.deepEqual(groupRolePermissions(catalog, ['dashboard:view', 'user:read']), [
    {
      label: '用户与组织',
      permissions: [{ key: 'user:read', name: '查看用户', group: '用户与组织' }]
    },
    {
      label: '统计分析',
      permissions: [
        { key: 'dashboard:view', name: '查看 Dashboard', group: '统计分析' }
      ]
    }
  ])
})

test('数据范围使用服务端目录中的中文名称', () => {
  const scopes = [
    { key: 'self', label: '仅本人' },
    { key: 'all', label: '全部数据' }
  ]

  assert.equal(getDataScopeLabel(scopes, 'all'), '全部数据')
  assert.equal(getDataScopeLabel(scopes, 'missing'), '未知范围')
})
