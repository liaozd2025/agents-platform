import assert from 'node:assert/strict'
import test from 'node:test'

import { getShareConfigLabel } from '../../src/utils/shareConfig.js'

test('共享标签明确表达组织子树语义', () => {
  assert.equal(
    getShareConfigLabel({
      version: 2,
      read_scope: { access_level: 'department', department_ids: [2] },
      manage_scope: null
    }),
    '只读组织节点及其下级(1)'
  )
})
