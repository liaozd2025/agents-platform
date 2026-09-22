import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  CHAT_MODEL_STORAGE_KEY,
  clearChatModelPreference,
  readChatModelPreference,
  resolveConversationModel,
  writeChatModelPreference
} from '../../src/utils/conversationModel.js'

const source = readFileSync(
  new URL('../../src/components/AgentChatComponent.vue', import.meta.url),
  'utf8'
)
const selectorSource = readFileSync(
  new URL('../../src/components/ModelSelectorComponent.vue', import.meta.url),
  'utf8'
)

// 用 Map 桩替换 localStorage：模型偏好读写不依赖浏览器环境，直接注入断言
const createStorage = (initial = {}) => {
  const values = new Map(Object.entries(initial))
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
    has: (key) => values.has(key)
  }
}

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

test('新会话继承本地偏好：新建会话不再回落到列表首项', () => {
  const modelBlock = source.slice(
    source.indexOf('const currentModelSpec = computed'),
    source.indexOf('const handleModelSelect')
  )

  // 解析链必须带上本地偏好，否则新会话拿不到上次手动选择的模型
  assert.match(modelBlock, /savedModel: savedChatModel\.value/)
  assert.match(source, /const savedChatModel = ref\(readChatModelPreference\(\)\)/)
  // 已有会话仍以自身 model_spec 为准，偏好只能排在会话绑定之后
  assert.equal(
    resolveConversationModel({
      conversationModel: 'conversation:model',
      savedModel: 'saved:model'
    }),
    'conversation:model'
  )
  // 新会话（无会话绑定、无显式选择）使用偏好
  assert.equal(resolveConversationModel({ savedModel: 'saved:model' }), 'saved:model')
})

test('只有手动选择才写入偏好，选择器兜底首项不写', () => {
  const selectBlock = source.slice(
    source.indexOf('const handleModelSelect'),
    source.indexOf('const configuredAgentToolApprovalMode')
  )

  assert.match(selectBlock, /if \(!options\.autoSelected\)/)
  assert.match(selectBlock, /writeChatModelPreference\(spec\)/)
  assert.match(selectBlock, /clearChatModelPreference\(\)/)
  // 兜底选择通过第二个参数标记来源，避免把「系统首项」固化成用户偏好
  assert.match(selectorSource, /emit\('select-model', firstModel\.spec, \{ autoSelected: true \}\)/)
})

test('模型偏好读写与清除走注入的存储，失败不抛异常', () => {
  const storage = createStorage()

  assert.equal(readChatModelPreference(storage), '')
  assert.equal(writeChatModelPreference('alibaba-cn:qwen3.7-max', storage), true)
  assert.equal(storage.has(CHAT_MODEL_STORAGE_KEY), true)
  assert.equal(readChatModelPreference(storage), 'alibaba-cn:qwen3.7-max')

  // 空值等价于清除偏好：下次新建会话回到列表首项
  assert.equal(writeChatModelPreference('', storage), true)
  assert.equal(readChatModelPreference(storage), '')

  assert.equal(writeChatModelPreference('manual:model', storage), true)
  assert.equal(clearChatModelPreference(storage), true)
  assert.equal(readChatModelPreference(storage), '')

  // 存储缺失（如 SSR / 无 localStorage）时读写都不得抛错
  assert.equal(readChatModelPreference(null), '')
  assert.equal(writeChatModelPreference('manual:model', null), false)
  assert.equal(clearChatModelPreference(null), false)
})

test('模型选择按当前选择、Conversation、本地偏好、空值的顺序解析', () => {
  assert.equal(
    resolveConversationModel({
      selectedModel: 'manual:model',
      conversationModel: 'conversation:model',
      savedModel: 'saved:model'
    }),
    'manual:model'
  )
  assert.equal(
    resolveConversationModel({ conversationModel: 'conversation:model', savedModel: 'saved:model' }),
    'conversation:model'
  )
  assert.equal(resolveConversationModel({ savedModel: 'saved:model' }), 'saved:model')
  assert.equal(resolveConversationModel({}), '')
})

test('发送请求使用当前展示模型并同步 Conversation metadata', () => {
  const sendBlock = source.slice(
    source.indexOf('const handleSendMessage'),
    source.indexOf('const handleDirectSteer')
  )

  assert.match(sendBlock, /const modelSpec = submission \? submission\.body\.model_spec : currentModelSpec\.value \|\| null/)
  assert.match(sendBlock, /model_spec: modelSpec/)
  assert.match(sendBlock, /status !== 'rejected' && modelSpec/)
  assert.match(sendBlock, /thread\.metadata = \{ \.\.\.\(thread\.metadata \|\| \{\}\), model_spec: modelSpec \}/)
})

test('路由选择即使已预写当前线程也会加载消息', () => {
  const routeSelectionBlock = source.slice(
    source.indexOf('const selectThreadFromRoute'),
    source.indexOf('const handleQuestionSubmit')
  )

  assert.doesNotMatch(
    routeSelectionBlock,
    /if \(currentThreadId\.value === threadId\) \{\s*return true\s*\}/
  )
  assert.match(routeSelectionBlock, /await selectChat\(threadId\)/)
})
