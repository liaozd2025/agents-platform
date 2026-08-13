import assert from 'node:assert/strict'
import test from 'node:test'

import {
  DEFAULT_OA_EMBED_MODE,
  createOAEmbedBridge,
  parseOAEmbedAllowedOrigins
} from '../../src/utils/oaEmbedBridge.js'
import { resolveAppSurface } from '../../src/composables/useEmbedMode.js'

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
  const createBridge = (options) =>
    createOAEmbedBridge({
      ...options,
      browserWindow,
      setTimer(callback) {
        timers.push(callback)
        return timers.length
      },
      clearTimer() {}
    })

  return {
    browserWindow,
    messages,
    timers,
    createBridge,
    dispatchMessage(event) {
      return messageListener(event)
    }
  }
}

test('OA origin config only keeps exact HTTP origins', () => {
  assert.equal(DEFAULT_OA_EMBED_MODE, 'fixed')
  assert.deepEqual(
    parseOAEmbedAllowedOrigins(
      'https://oa.example.test, http://localhost:4173 https://oa.example.test/path ftp://oa.test *'
    ),
    ['https://oa.example.test', 'http://localhost:4173']
  )
})

test('route metadata is the only standalone and OA embed surface boundary', () => {
  assert.equal(resolveAppSurface({ matched: [{ meta: { embed: true } }] }), 'oa-embed')
  assert.equal(resolveAppSurface({ matched: [{ meta: { requiresAuth: true } }] }), 'standalone')
})

test('OA bridge only accepts a token from an allowed parent origin', async () => {
  const harness = createBrowserHarness()
  const acceptedTokens = []
  const bridge = harness.createBridge({
    allowedOrigins: ['https://oa.example.test'],
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
  const bridge = harness.createBridge({
    allowedOrigins: ['https://oa.example.test'],
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

test('OA bridge validates all mode and close messages at the authenticated parent boundary', async () => {
  const harness = createBrowserHarness()
  const confirmedModes = []
  const bridge = harness.createBridge({
    allowedOrigins: ['https://oa.example.test', 'https://oa-backup.example.test'],
    onToken: async () => {},
    onModeChanged: (mode) => confirmedModes.push(mode)
  })

  bridge.start()
  await harness.dispatchMessage({
    source: harness.browserWindow.parent,
    origin: 'https://oa-backup.example.test',
    data: { type: 'oa:token', token: 'yuxi-token' }
  })
  bridge.requestAuthRequired()
  assert.equal(bridge.requestMode('fixed'), true)
  assert.equal(bridge.requestMode('floating', 'thread-1'), true)
  assert.equal(bridge.requestMode('fullscreen', 'thread-1'), true)
  assert.equal(bridge.requestMode('invalid', 'thread-1'), false)
  bridge.requestClose('thread-1')
  await harness.dispatchMessage({
    source: {},
    origin: 'https://oa-backup.example.test',
    data: { type: 'oa:mode-changed', mode: 'fixed' }
  })
  await harness.dispatchMessage({
    source: harness.browserWindow.parent,
    origin: 'https://attacker.example.test',
    data: { type: 'oa:mode-changed', mode: 'fullscreen' }
  })
  await harness.dispatchMessage({
    source: harness.browserWindow.parent,
    origin: 'https://oa-backup.example.test',
    data: { type: 'oa:mode-changed', mode: 'invalid' }
  })
  await harness.dispatchMessage({
    source: harness.browserWindow.parent,
    origin: 'https://oa-backup.example.test',
    data: { type: 'oa:mode-changed', mode: 'fullscreen' }
  })

  assert.deepEqual(harness.messages.slice(-5), [
    {
      message: { type: 'yuxi:auth-required' },
      targetOrigin: 'https://oa-backup.example.test'
    },
    {
      message: { type: 'yuxi:mode-request', mode: 'fixed' },
      targetOrigin: 'https://oa-backup.example.test'
    },
    {
      message: { type: 'yuxi:mode-request', mode: 'floating', threadId: 'thread-1' },
      targetOrigin: 'https://oa-backup.example.test'
    },
    {
      message: { type: 'yuxi:mode-request', mode: 'fullscreen', threadId: 'thread-1' },
      targetOrigin: 'https://oa-backup.example.test'
    },
    {
      message: { type: 'yuxi:close-request', threadId: 'thread-1' },
      targetOrigin: 'https://oa-backup.example.test'
    }
  ])
  assert.deepEqual(confirmedModes, ['fullscreen'])
  assert.equal(
    harness.messages.some(({ targetOrigin }) => targetOrigin === '*'),
    false
  )
})
