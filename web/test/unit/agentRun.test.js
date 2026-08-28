import assert from 'node:assert/strict'
import test from 'node:test'

import { getAgentRunStatusView, isSteerableMainChatRun } from '../../src/utils/agentRun.js'

test('Steer is exposed only for running main Chat requests', () => {
  assert.equal(
    isSteerableMainChatRun({ status: 'running', run_type: 'chat', source: 'chat' }),
    true
  )
  assert.equal(isSteerableMainChatRun({ status: 'pending', run_type: 'chat', source: 'chat' }), false)
  assert.equal(isSteerableMainChatRun({ status: 'running', run_type: 'resume', source: 'chat' }), false)
  assert.equal(
    isSteerableMainChatRun({ status: 'running', run_type: 'chat', source: 'agent_call' }),
    false
  )
})

test('Agent Run 审计状态使用执行状态而不是会话生命周期', () => {
  assert.deepEqual(getAgentRunStatusView('running'), { label: '进行中', color: 'green' })
  assert.deepEqual(getAgentRunStatusView('completed'), { label: '已完成', color: 'blue' })
  assert.deepEqual(getAgentRunStatusView('failed'), { label: '失败', color: 'red' })
  assert.deepEqual(getAgentRunStatusView(null), { label: '未运行', color: 'default' })
})
