import assert from 'node:assert/strict'
import test from 'node:test'
import { normalizeModelConfig } from '../../src/utils/modelMetadata.js'

test('model edits preserve explicit per-model capabilities and unknown stays unknown', () => {
  const model = {
    id: 'm',
    type: 'chat',
    context_length: 32768,
    max_completion_tokens: 4096,
    input_modalities: ['text', 'image'],
    reasoning: false
  }
  const payload = normalizeModelConfig(normalizeModelConfig(model))
  for (const key of Object.keys(model)) assert.deepEqual(payload[key], model[key])
  const unknown = normalizeModelConfig({ id: 'n' })
  assert.equal(unknown.reasoning, null)
  assert.equal(unknown.max_completion_tokens, null)
  assert.deepEqual(unknown.input_modalities, [])
})
