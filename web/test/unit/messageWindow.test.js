import assert from 'node:assert/strict'
import test from 'node:test'
import { sliceMessageRows } from '../../src/utils/messageWindow.js'

test('最近窗口限制跨轮次和单轮旧历史，扩展后保留原顺序与事实引用', () => {
  const conv = { sources: ['original-source'] }
  const items = Array.from({ length: 500 }, (_, key) => ({ key, type: 'message' }))
  const rows = [{ key: 'history', conv, displayItems: items }]
  const recent = sliceMessageRows(rows, 20)
  assert.equal(recent.hasEarlier, true)
  assert.deepEqual(recent.rows[0].displayItems, items.slice(-20))
  assert.equal(recent.rows[0].conv, conv)
  assert.deepEqual(sliceMessageRows(rows, 500), { rows, hasEarlier: false })
  const rounds = items.map((item) => ({ key: item.key, displayItems: [item] }))
  assert.deepEqual(sliceMessageRows(rounds, 20).rows, rounds.slice(-20))
})

test('无消息的运行和配置通知也占据窗口，最新流式条目不丢失', () => {
  const rows = [
    { key: 'old', displayItems: [{ key: 'a' }] },
    { key: 'failed', displayItems: [] },
    { key: 'notice', notice: {} },
    { key: 'live', displayItems: [{ key: 'latest' }] }
  ]
  assert.deepEqual(sliceMessageRows(rows, 3), { rows: rows.slice(1), hasEarlier: true })
  assert.deepEqual(sliceMessageRows([], 20), { rows: [], hasEarlier: false })
})
