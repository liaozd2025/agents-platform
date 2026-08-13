import assert from 'node:assert/strict'
import test from 'node:test'

import {
  requestOAEmbedAuthentication,
  setOAEmbedAuthRequiredHandler
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
