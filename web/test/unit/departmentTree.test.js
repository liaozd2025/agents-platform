import assert from 'node:assert/strict'
import test from 'node:test'

import { buildDepartmentTree, getDepartmentSubtreeIds } from '../../src/utils/departmentTree.js'

test('组织节点列表可组装为树且移动时禁用自身与后代', () => {
  const departments = [
    { id: 4, parent_id: 2, name: '研发部' },
    { id: 1, parent_id: null, name: '集团' },
    { id: 3, parent_id: 1, name: '华南公司' },
    { id: 2, parent_id: 1, name: '华东公司' }
  ]
  const disabledIds = getDepartmentSubtreeIds(departments, 2)
  const tree = buildDepartmentTree(departments, disabledIds)

  assert.deepEqual([...disabledIds].sort(), [2, 4])
  assert.deepEqual(tree, [
    {
      id: 1,
      parent_id: null,
      name: '集团',
      children: [
        {
          id: 3,
          parent_id: 1,
          name: '华南公司'
        },
        {
          id: 2,
          parent_id: 1,
          name: '华东公司',
          disabled: true,
          children: [
            {
              id: 4,
              parent_id: 2,
              name: '研发部',
              disabled: true
            }
          ]
        }
      ]
    }
  ])
})
