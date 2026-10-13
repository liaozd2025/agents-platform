import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  CHAT_MODEL_STORAGE_KEY,
  clearChatModelPreference,
  extractModelName,
  isModelSpecAvailable,
  readChatModelPreference,
  resolveConversationModel,
  resolveFallbackModelSpec,
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

// 构造与后端 /models/v2 结构一致的模型列表
const createV2Models = (specsByProvider) =>
  Object.fromEntries(
    Object.entries(specsByProvider).map(([providerId, specs]) => [
      providerId,
      { models: specs.map((spec) => ({ spec, model_id: spec.split(':')[1] })) }
    ])
  )

const listWithFlash = createV2Models({
  'alibaba-cn': ['alibaba-cn:qwen3.7-max', 'alibaba-cn:qwen3.7-plus'],
  'siliconflow-cn': ['siliconflow-cn:deepseek-ai/DeepSeek-V4-Flash']
})

// 下架 DeepSeek-V4-Flash 之后的列表：偏好与会话 metadata 仍指向它
const listAfterFlashRemoved = createV2Models({
  'alibaba-cn': ['alibaba-cn:qwen3.7-max', 'alibaba-cn:qwen3.7-plus']
})

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

test('extractModelName 只切供应商段，保留模型名中的斜杠', () => {
  assert.equal(extractModelName('alibaba-cn:qwen3.7-max'), 'qwen3.7-max')
  assert.equal(
    extractModelName('siliconflow-cn:deepseek-ai/DeepSeek-V4-Flash'),
    'deepseek-ai/DeepSeek-V4-Flash'
  )
  // 无冒号时原样返回；空值不抛错
  assert.equal(extractModelName('no-colon-model'), 'no-colon-model')
  assert.equal(extractModelName(''), '')
  assert.equal(extractModelName(undefined), '')
})

test('模型仍在列表中时判定为可用', () => {
  assert.equal(
    isModelSpecAvailable(listWithFlash, 'siliconflow-cn:deepseek-ai/DeepSeek-V4-Flash'),
    true
  )
  assert.equal(isModelSpecAvailable(listWithFlash, 'alibaba-cn:qwen3.7-max'), true)
})

test('模型从列表移除后判定为失效（DeepSeek 下架场景）', () => {
  // 偏好仍指向 DeepSeek，但列表里已经没有它 → 必须判定为失效
  assert.equal(
    isModelSpecAvailable(listAfterFlashRemoved, 'siliconflow-cn:deepseek-ai/DeepSeek-V4-Flash'),
    false
  )
  // 未下架的模型不受影响
  assert.equal(isModelSpecAvailable(listAfterFlashRemoved, 'alibaba-cn:qwen3.7-max'), true)
})

test('空 spec 与异常列表结构不误判为失效', () => {
  // 空选择由兜底首项逻辑处理，不是失效场景
  assert.equal(isModelSpecAvailable(listWithFlash, ''), false)
  // 列表未加载 / 结构异常时不得把用户模型误判成下架
  assert.equal(isModelSpecAvailable(null, 'alibaba-cn:qwen3.7-max'), false)
  assert.equal(isModelSpecAvailable(undefined, 'alibaba-cn:qwen3.7-max'), false)
  assert.equal(isModelSpecAvailable({}, 'alibaba-cn:qwen3.7-max'), false)
  assert.equal(isModelSpecAvailable({ p: null }, 'alibaba-cn:qwen3.7-max'), false)
  assert.equal(isModelSpecAvailable({ p: {} }, 'alibaba-cn:qwen3.7-max'), false)
  assert.equal(isModelSpecAvailable({ p: { models: 'oops' } }, 'alibaba-cn:qwen3.7-max'), false)
})

test('失效回退取列表首项，与选择器兜底首项语义一致', () => {
  assert.equal(resolveFallbackModelSpec(listAfterFlashRemoved), 'alibaba-cn:qwen3.7-max')
  // 列表顺序即供应商顺序，不做额外排序
  assert.equal(
    resolveFallbackModelSpec(
      createV2Models({ 'siliconflow-cn': ['siliconflow-cn:zai-org/GLM-5.2'], 'alibaba-cn': ['alibaba-cn:qwen3.7-max'] })
    ),
    'siliconflow-cn:zai-org/GLM-5.2'
  )
  // 无任何可用模型时返回空串，调用方据此保持原状而不是替换成非法值
  assert.equal(resolveFallbackModelSpec({}), '')
  assert.equal(resolveFallbackModelSpec(null), '')
})

test('选择器在拿到列表后校验当前模型并在失效时回退', () => {
  const recoverBlock = selectorSource.slice(
    selectorSource.indexOf('const recoverFromUnavailableModel'),
    selectorSource.indexOf('const initializeDefaultModel')
  )

  // 失效判定必须以「列表已成功拉取」为前提，避免首屏竞态误换模型。
  // 判据只能是 hasFetchedV2Models：不能用 loadingV2Models 兜底，因为判定发生在
  // fetchV2Models 成功回调内，而 loadingV2Models 到 finally 才置 false，回调期间为 true。
  assert.match(selectorSource, /const hasFetchedV2Models = ref\(false\)/)
  assert.match(selectorSource, /hasFetchedV2Models\.value = true/)
  assert.match(
    selectorSource,
    /const isCurrentModelUnavailable = \(\) => \{[\s\S]*?if \(!hasFetchedV2Models\.value\) return false/
  )
  assert.match(selectorSource, /!isModelSpecAvailable\(v2Models\.value, props\.model_spec\)/)
  // 精确锁死：判定函数体内不得出现 loadingV2Models。
  // 这里不能用宽松的跨段正则——那会跨过整个函数体，挡不住把 loading 守卫加回来的回归。
  const unavailableBody = selectorSource.slice(
    selectorSource.indexOf('const isCurrentModelUnavailable = () => {'),
    selectorSource.indexOf('// 聊天输入框没有任何默认模型时')
  )
  assert.doesNotMatch(unavailableBody, /loadingV2Models/)
  assert.match(unavailableBody, /if \(!props\.model_spec\) return false/)

  // 回退由独立的 fallbackWhenUnavailable 控制：autoSelectFirst 表达的是「没有选择时兜底」，
  // 而聊天页传 !currentModelSpec，偏好失效时它恰好为 false，不能兼作场景开关
  assert.match(recoverBlock, /if \(!props\.fallbackWhenUnavailable \|\| props\.disabled\) return/)
  assert.doesNotMatch(recoverBlock, /props\.autoSelectFirst/)
  // 无可用模型时保持原状
  assert.match(recoverBlock, /if \(!fallbackSpec\) \{[\s\S]*?return/)
  // 幂等：同一会话内对同一失效 spec 只回退一次，避免列表重复拉取导致选择抖动
  assert.match(recoverBlock, /if \(recoveredUnavailableSelections\.has\(scope\)\) return/)

  // 副作用挂在列表拉取成功回调上：唯一同时持有当前 spec 与权威列表的位置
  assert.match(
    selectorSource,
    /hasFetchedV2Models\.value = true[\s\S]*?applyModelListSideEffects\(\)/
  )
  assert.match(
    selectorSource,
    /const applyModelListSideEffects = \(\) => \{[\s\S]*?selectFirstModelIfNeeded\(\)[\s\S]*?recoverFromUnavailableModel\(\)/
  )
  // initializeDefaultModel 不得重复触发副作用，否则并发去重会二次 emit
  const initBlock = selectorSource.slice(
    selectorSource.indexOf('const initializeDefaultModel'),
    selectorSource.indexOf('// 下拉展开前先刷新模型列表')
  )
  assert.match(initBlock, /await fetchV2Models\(\)/)
  assert.doesNotMatch(initBlock, /applyModelListSideEffects|selectFirstModelIfNeeded/)
})

test('偏好失效时 autoSelectFirst 为 false 仍能拉到列表并回退', () => {
  // 聊天页传 :auto-select-first="!currentModelSpec"，模型下架后 currentModelSpec 非空
  // → autoSelectFirst 为 false。若列表加载只认 autoSelectFirst，就永远拿不到权威列表，
  // 失效回退不会触发，整条修复静默失效。这里锁死两个条件。
  const initBlock = selectorSource.slice(
    selectorSource.indexOf('const initializeDefaultModel'),
    selectorSource.indexOf('// 下拉展开前先刷新模型列表')
  )
  assert.match(initBlock, /if \(!props\.autoSelectFirst && !props\.fallbackWhenUnavailable\) return/)

  // watch 必须监听 fallbackWhenUnavailable，且首屏 immediate 就能触发
  const watchBlock = selectorSource.slice(
    selectorSource.lastIndexOf('watch('),
    selectorSource.indexOf('// 清空选择')
  )
  assert.match(watchBlock, /props\.fallbackWhenUnavailable/)
  assert.match(watchBlock, /\{ immediate: true \}/)

  // 聊天页必须显式开启该开关，否则组件默认值 false 让回退不生效
  const selectorStart = source.indexOf('<ModelSelectorComponent')
  const chatSelectorBlock = source.slice(
    selectorStart,
    source.indexOf('@select-model', selectorStart)
  )
  assert.match(chatSelectorBlock, /fallback-when-unavailable/)
})

test('幂等去重按会话范围，跨会话的第二个失效会话仍会回退', () => {
  // 切换会话时组件实例复用，两个会话都绑同一个已下架模型时 model_spec 完全相同。
  // 若幂等 Set 只按 spec 去重，先前会话回退过后，第二个会话会被误判为「已处理」
  // 而不再回退，界面重新显示已下架的模型名（实测复现：A 切换正常 → 侧栏切到 B 仍显示 3.7-max）。
  const recoverBlock = selectorSource.slice(
    selectorSource.indexOf('const recoverFromUnavailableModel'),
    selectorSource.indexOf('// 列表拉取成功后统一处理')
  )
  // 幂等键必须包含 selectionScope
  assert.match(recoverBlock, /const scope = `\$\{props\.selectionScope\}::\$\{unavailableSpec\}`/)
  assert.match(recoverBlock, /recoveredUnavailableSelections\.has\(scope\)/)
  assert.match(recoverBlock, /recoveredUnavailableSelections\.add\(scope\)/)
  // 不得退回只按 spec 去重
  assert.doesNotMatch(selectorSource, /recoveredUnavailableSelections\.(has|add)\(unavailableSpec\)/)

  // 组件必须声明该 prop
  assert.match(selectorSource, /selectionScope:\s*\{\s*type: String/)

  // 聊天页必须把会话 id 传进去
  const selectorStart = source.indexOf('<ModelSelectorComponent')
  const chatSelectorBlock = source.slice(
    selectorStart,
    source.indexOf('@select-model', selectorStart)
  )
  assert.match(chatSelectorBlock, /:selection-scope="currentChatId \|\| DRAFT_MODEL_KEY"/)
})

test('切换会话时重新判定失效，不依赖再次拉取列表', () => {
  // 切换会话不会重新请求模型列表（fetchV2Models 有并发去重，且列表内容没变），
  // 所以挂在 fetchV2Models 成功回调里的回退不会再次触发。
  // 结果：先打开的会话回退正常，侧栏切到另一个同样绑定已下架模型的会话时仍显示旧模型名
  // （实测复现：A 显示 plus，切到 B 又变回 3.7-max）。必须在 selectionScope 变化时补一次判定。
  const scopeWatch = selectorSource.slice(
    selectorSource.indexOf('() => [props.selectionScope, props.model_spec]'),
    selectorSource.indexOf('const resolvedSize = computed')
  )
  assert.match(scopeWatch, /\(\) => \[props\.selectionScope, props\.model_spec\]/)
  assert.match(scopeWatch, /recoveredUnavailableSelections\.clear\(\)/)
  // 复用已加载的列表，不重新请求
  assert.match(scopeWatch, /if \(!hasFetchedV2Models\.value\) return/)
  assert.match(scopeWatch, /recoverFromUnavailableModel\(\)/)
  assert.doesNotMatch(scopeWatch, /fetchV2Models\(\)/)
})

test('回退结果带 autoSelected 与失效来源，不污染用户偏好', () => {
  assert.match(
    selectorSource,
    /emit\('select-model', fallbackSpec, \{ autoSelected: true, unavailableSpec \}\)/
  )

  const selectBlock = source.slice(
    source.indexOf('const handleModelSelect'),
    source.indexOf('const configuredAgentToolApprovalMode')
  )
  // 失效回退同样不得写入偏好，否则失效模型名会被永久固化成用户偏好
  assert.match(selectBlock, /if \(!options\.autoSelected\) \{[\s\S]*?writeChatModelPreference\(spec\)/)
  // 聊天页对失效回退给出可见提示，而不是静默换模型
  assert.match(selectBlock, /options\.unavailableSpec/)
  assert.match(selectBlock, /message\.warning\(/)
})
