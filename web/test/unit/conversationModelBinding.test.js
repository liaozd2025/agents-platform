import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { resolveConversationModel } from '../../src/utils/conversationModel.js'

const source = readFileSync(
  new URL('../../src/components/AgentChatComponent.vue', import.meta.url),
  'utf8'
)

test('新会话不再硬编码未配置模型，等待选择器提供已配置模型', () => {
  assert.equal(resolveConversationModel({}), '')
})

test('新会话开启已配置模型首项自动选择，已有会话不受影响', () => {
  const selectorStart = source.indexOf('<ModelSelectorComponent')
  const modelBlock = source.slice(
    source.indexOf('const currentModelSpec = computed'),
    source.indexOf('const handleModelSelect')
  )
  const selectorBlock = source.slice(
    selectorStart,
    source.indexOf('@select-model', selectorStart)
  )

  assert.match(modelBlock, /resolveConversationModel\(\{/)
  assert.doesNotMatch(modelBlock, /agentConfig|currentAgent|configStore/)
  assert.match(selectorBlock, /:auto-select-first="!currentModelSpec"/)
  assert.match(source, /!isProcessing\.value && !currentModelSpec\.value/)
})

test('模型选择按当前选择、Conversation、空值的顺序解析', () => {
  assert.equal(
    resolveConversationModel({
      selectedModel: 'manual:model',
      conversationModel: 'conversation:model'
    }),
    'manual:model'
  )
  assert.equal(
    resolveConversationModel({ conversationModel: 'conversation:model' }),
    'conversation:model'
  )
  assert.equal(resolveConversationModel({}), '')
})

test('发送请求使用当前展示模型并同步 Conversation metadata', () => {
  const sendBlock = source.slice(
    source.indexOf('const handleSendMessage'),
    source.indexOf('const handleDirectSteer')
  )

  assert.match(sendBlock, /const modelSpec = currentModelSpec\.value \|\| null/)
  assert.match(sendBlock, /model_spec: modelSpec/)
  assert.match(sendBlock, /status !== 'rejected' && modelSpec/)
  assert.match(sendBlock, /thread\.metadata = \{ \.\.\.\(thread\.metadata \|\| \{\}\), model_spec: modelSpec \}/)
})
