import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  DEFAULT_CHAT_MODEL,
  resolveConversationModel
} from '../../src/utils/conversationModel.js'

const source = readFileSync(
  new URL('../../src/components/AgentChatComponent.vue', import.meta.url),
  'utf8'
)

test('OA iframe 新会话使用指定的 DashScope 默认模型', () => {
  assert.equal(DEFAULT_CHAT_MODEL, 'alibaba-cn:qwen3.7-max')
  assert.equal(resolveConversationModel({}), DEFAULT_CHAT_MODEL)
})

test('独立登录新会话使用 DashScope 默认模型，不受异步配置加载影响', () => {
  const selectorStart = source.indexOf('<ModelSelectorComponent')
  const modelBlock = source.slice(
    source.indexOf('const currentModelSpec = computed'),
    source.indexOf('const handleModelSelect')
  )
  const selectorBlock = source.slice(
    selectorStart,
    source.indexOf('@select-model', selectorStart)
  )

  assert.equal(
    resolveConversationModel({
      agentModel: 'alibaba-cn:qwen3.7-max',
      systemDefaultModel: 'alibaba-cn:qwen3.7-max'
    }),
    DEFAULT_CHAT_MODEL
  )
  assert.match(modelBlock, /resolveConversationModel\(\{/)
  assert.doesNotMatch(modelBlock, /agentConfig|currentAgent|configStore/)
  assert.doesNotMatch(selectorBlock, /auto-select-first/)
})

test('模型选择按当前选择、Conversation、DashScope 默认的顺序解析', () => {
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
})

test('发送当前展示模型并在请求被接受后同步 Conversation metadata', () => {
  const sendBlock = source.slice(
    source.indexOf('const handleSendMessage'),
    source.indexOf('const handleDirectSteer')
  )

  assert.match(sendBlock, /const modelSpec = currentModelSpec\.value \|\| null/)
  assert.match(sendBlock, /model_spec: modelSpec/)
  assert.match(sendBlock, /status !== 'rejected' && modelSpec/)
  assert.match(sendBlock, /thread\.metadata = \{ \.\.\.\(thread\.metadata \|\| \{\}\), model_spec: modelSpec \}/)
})
