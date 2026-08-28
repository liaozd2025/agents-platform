import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
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

test('部门树展开时选择浮层由组件状态保持打开', () => {
  const source = readFileSync(
    new URL('../../src/components/ShareConfigForm.vue', import.meta.url),
    'utf8'
  )

  // Dropdown 受控后，树节点点击只更新展开状态，不会被组件内部的 overlay click 自动关闭。
  assert.match(
    source,
    /v-model:open="selectionDropdownOpen\[scope\.key\]\[option\.value\]"/
  )
})
