import assert from 'node:assert/strict'
import test from 'node:test'

import { createOAEmbedBridge, parseOAEmbedAllowedOrigins } from '../../src/utils/oaEmbedBridge.js'
import { routeUsesEmbedMode } from '../../src/composables/useEmbedMode.js'

function createBrowserHarness() {
  const messages = []
  const timers = []
  let messageListener = null
  const parent = {
    postMessage(message, targetOrigin) {
      messages.push({ message, targetOrigin })
    }
  }
  const browserWindow = {
    parent,
    addEventListener(type, listener) {
      if (type === 'message') messageListener = listener
    },
    removeEventListener(type, listener) {
      if (type === 'message' && messageListener === listener) messageListener = null
    }
  }

  return {
    browserWindow,
    messages,
    timers,
    dispatchMessage(event) {
      return messageListener(event)
    },
    setTimer(callback) {
      timers.push(callback)
      return timers.length
    },
    clearTimer() {}
  }
}

test('OA origin config only keeps exact HTTP origins', () => {
  assert.deepEqual(
    parseOAEmbedAllowedOrigins(
      'https://oa.example.test, http://localhost:4173 https://oa.example.test/path ftp://oa.test *'
    ),
    ['https://oa.example.test', 'http://localhost:4173']
  )
})

test('OA startup mode is derived from matched route metadata', () => {
  assert.equal(routeUsesEmbedMode({ matched: [{ meta: { embed: true } }] }), true)
  assert.equal(routeUsesEmbedMode({ matched: [{ meta: { requiresAuth: true } }] }), false)
})

test('OA bridge only accepts bearer from an allowed parent origin', async () => {
  const harness = createBrowserHarness()
  const acceptedTokens = []
  const bridge = createOAEmbedBridge({
    allowedOrigins: ['https://oa.example.test'],
    browserWindow: harness.browserWindow,
    setTimer: harness.setTimer,
    clearTimer: harness.clearTimer,
    onToken: async (token) => acceptedTokens.push(token)
  })

  bridge.start()
  await harness.dispatchMessage({
    source: harness.browserWindow.parent,
    origin: 'https://attacker.example.test',
    data: { type: 'oa:token', token: 'attacker-token' }
  })
  await harness.dispatchMessage({
    source: harness.browserWindow.parent,
    origin: 'https://oa.example.test',
    data: { type: 'oa:token', token: 'yuxi-token' }
  })

  assert.deepEqual(acceptedTokens, ['yuxi-token'])
  assert.deepEqual(harness.messages[0], {
    message: { type: 'yuxi:ready' },
    targetOrigin: 'https://oa.example.test'
  })
  assert.equal(
    harness.messages.some(({ targetOrigin }) => targetOrigin === '*'),
    false
  )
})

test('OA bridge retries ready once when the parent listener is late', () => {
  const harness = createBrowserHarness()
  const bridge = createOAEmbedBridge({
    allowedOrigins: ['https://oa.example.test'],
    browserWindow: harness.browserWindow,
    setTimer: harness.setTimer,
    clearTimer: harness.clearTimer,
    onToken: async () => {}
  })

  bridge.start()
  harness.timers[0]()

  assert.deepEqual(
    harness.messages.map(({ message }) => message.type),
    ['yuxi:ready', 'yuxi:ready']
  )
  assert.equal(harness.timers.length, 1)
})

test('OA bridge pins auth-required and expand messages to the authenticated parent', async () => {
  const harness = createBrowserHarness()
  const bridge = createOAEmbedBridge({
    allowedOrigins: ['https://oa.example.test', 'https://oa-backup.example.test'],
    browserWindow: harness.browserWindow,
    setTimer: harness.setTimer,
    clearTimer: harness.clearTimer,
    onToken: async () => {}
  })

  bridge.start()
  await harness.dispatchMessage({
    source: harness.browserWindow.parent,
    origin: 'https://oa-backup.example.test',
    data: { type: 'oa:token', token: 'yuxi-token' }
  })
  bridge.requestAuthRequired()
  bridge.expand('thread-1')

  assert.deepEqual(harness.messages.slice(-2), [
    {
      message: { type: 'yuxi:auth-required' },
      targetOrigin: 'https://oa-backup.example.test'
    },
    {
      message: { type: 'yuxi:expand', threadId: 'thread-1' },
      targetOrigin: 'https://oa-backup.example.test'
    }
  ])
})
