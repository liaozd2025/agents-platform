import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildDepartmentShareTree,
  buildDepartmentTree,
  getDepartmentExpandableKeys,
  getDepartmentSelectionSummary,
  isDepartmentSelectionCovered,
  normalizeDepartmentSelection,
  updateDepartmentExpandedKeys
} from '../../src/utils/departmentTree.js'

test('组织节点列表可组装为树且移动时禁用自身与后代', () => {
  const departments = [
    { id: 4, parent_id: 2, name: '研发部' },
    { id: 1, parent_id: null, name: '集团' },
    { id: 3, parent_id: 1, name: '华南公司' },
    { id: 2, parent_id: 1, name: '华东公司' }
  ]
  const tree = buildDepartmentTree(departments, 2)

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

test('数百个组织节点可一次线性组装且保持完整', () => {
  const departments = [
    { id: 1, parent_id: null, name: '集团' },
    ...Array.from({ length: 600 }, (_, index) => ({
      id: index + 2,
      parent_id: 1,
      name: `组织节点 ${index + 1}`
    }))
  ]

  const tree = buildDepartmentTree(departments)

  assert.equal(tree.length, 1)
  assert.equal(tree[0].children.length, 600)
})

test('展开全部只包含拥有子节点的组织节点', () => {
  const departments = [
    { id: 1, parent_id: null, name: '集团' },
    { id: 2, parent_id: 1, name: '华东公司' },
    { id: 3, parent_id: 2, name: '研发部' },
    { id: 4, parent_id: 1, name: '华南公司' }
  ]

  assert.deepEqual(getDepartmentExpandableKeys(departments), [1, 2])
})

test('节点图标可逐项展开和收起受控树表', () => {
  assert.deepEqual(updateDepartmentExpandedKeys([1], 2, true), [1, 2])
  assert.deepEqual(updateDepartmentExpandedKeys([1, 2], 1, false), [2])
})

test('共享范围树会禁用已选节点的全部后代', () => {
  const departments = [
    { id: 1, parent_id: null, name: '集团' },
    { id: 2, parent_id: 1, name: '华东公司' },
    { id: 3, parent_id: 2, name: '研发部' },
    { id: 4, parent_id: 1, name: '华南公司' }
  ]

  assert.deepEqual(buildDepartmentShareTree(departments, [2]), [
    {
      id: 1,
      parent_id: null,
      name: '集团',
      key: 1,
      title: '集团',
      children: [
        {
          id: 2,
          parent_id: 1,
          name: '华东公司',
          key: 2,
          title: '华东公司',
          children: [
            {
              id: 3,
              parent_id: 2,
              name: '研发部',
              key: 3,
              title: '研发部',
              disabled: true,
              inherited: true
            }
          ]
        },
        {
          id: 4,
          parent_id: 1,
          name: '华南公司',
          key: 4,
          title: '华南公司'
        }
      ]
    }
  ])
})

test('共享范围树搜索保留命中节点的祖先链', () => {
  const departments = [
    { id: 1, parent_id: null, name: '集团' },
    { id: 2, parent_id: 1, name: '华东公司' },
    { id: 3, parent_id: 2, name: '研发部' },
    { id: 4, parent_id: 1, name: '华南公司' }
  ]

  const tree = buildDepartmentShareTree(departments, [], '研发')

  assert.equal(tree.length, 1)
  assert.equal(tree[0].children.length, 1)
  assert.equal(tree[0].children[0].children[0].name, '研发部')
})

test('选中上级节点时移除已选的后代节点', () => {
  const departments = [
    { id: 1, parent_id: null, name: '集团' },
    { id: 2, parent_id: 1, name: '华东公司' },
    { id: 3, parent_id: 2, name: '研发部' }
  ]

  assert.deepEqual(normalizeDepartmentSelection(departments, [3, 1, 2]), [1])
})

test('管理节点只能位于任一读取节点的子树内', () => {
  const departments = [
    { id: 1, parent_id: null, name: '集团' },
    { id: 2, parent_id: 1, name: '华东公司' },
    { id: 3, parent_id: 2, name: '研发部' },
    { id: 4, parent_id: 1, name: '华南公司' }
  ]

  assert.equal(isDepartmentSelectionCovered(departments, [1], [2, 3]), true)
  assert.equal(isDepartmentSelectionCovered(departments, [2], [1]), false)
  assert.equal(isDepartmentSelectionCovered(departments, [2], [4]), false)
})

test('共享范围摘要表达组织子树语义', () => {
  const departments = [
    { id: 1, parent_id: null, name: '集团' },
    { id: 2, parent_id: 1, name: '华东公司' },
    { id: 3, parent_id: 1, name: '华南公司' }
  ]

  assert.equal(getDepartmentSelectionSummary(departments, [2]), '华东公司及其下级组织')
  assert.equal(
    getDepartmentSelectionSummary(departments, [2, 3]),
    '华东公司、华南公司等 2 个组织子树'
  )
})
