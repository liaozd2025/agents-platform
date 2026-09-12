import assert from 'node:assert/strict'
import test from 'node:test'

import {
  requestOAEmbedAuthentication,
  requestOAEmbedNavigate,
  setOAEmbedAuthRequiredHandler,
  setOAEmbedNavigateHandler
} from '../../src/utils/oaEmbedSession.js'

test('OA embed session only handles reauthentication while a bridge is active', () => {
  let requests = 0
  const clear = setOAEmbedAuthRequiredHandler(() => {
    requests += 1
  })

  assert.equal(requestOAEmbedAuthentication(), true)
  clear()
  assert.equal(requestOAEmbedAuthentication(), false)
  assert.equal(requests, 1)
})

test('OA embed session forwards navigation only while a bridge is active', () => {
  const navigations = []
  const clear = setOAEmbedNavigateHandler((params) => {
    navigations.push(params)
    return true
  })

  assert.equal(requestOAEmbedNavigate({ taskId: '2441705', ecType: '3' }), true)
  clear()
  // 无活动嵌入会话时返回 false，调用方据此降级为新标签打开
  assert.equal(requestOAEmbedNavigate({ taskId: '2441705', ecType: '3' }), false)
  assert.deepEqual(navigations, [{ taskId: '2441705', ecType: '3' }])
})
