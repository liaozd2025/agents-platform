/**
 * 聊天页模型选择的偏好读写与解析。
 *
 * 解析顺序：当前会话的显式选择 → 会话自身绑定的模型 → 用户最近一次手动选择（本地偏好）→ 空值。
 * 偏好存在的意义：新建会话不再回落到「已配置模型列表首项」，而是沿用用户上次手动选择的模型；
 * 已有会话始终以自己的 model_spec 为准，不受偏好影响。
 * 偏好为空时（从未手动选择过）仍由 ModelSelectorComponent 兜底选择列表首项。
 */

// 本地偏好键名；与 toolApproval.js 的命名风格保持一致
export const CHAT_MODEL_STORAGE_KEY = 'yuxi_chat_model'

const resolveStorage = (storage) =>
  storage || (typeof window !== 'undefined' ? window.localStorage : null)

/**
 * 读取用户最近一次手动选择的模型。
 * @param {Storage} [storage] 可注入的存储实现，默认使用全局 localStorage
 * @returns {string} 模型 spec；无偏好或读取失败时返回空字符串
 */
export const readChatModelPreference = (storage) => {
  const targetStorage = resolveStorage(storage)
  if (!targetStorage) return ''

  try {
    return targetStorage.getItem(CHAT_MODEL_STORAGE_KEY) || ''
  } catch {
    // 隐私模式等场景下 localStorage 可能抛错，读取失败按无偏好处理
    return ''
  }
}

/**
 * 清除模型偏好：下次新建会话重新回到「已配置模型列表首项」。
 * @param {Storage} [storage] 可注入的存储实现，默认使用全局 localStorage
 * @returns {boolean} 是否清除成功
 */
export const clearChatModelPreference = (storage) => {
  const targetStorage = resolveStorage(storage)
  if (!targetStorage) return false

  try {
    targetStorage.removeItem(CHAT_MODEL_STORAGE_KEY)
    return true
  } catch {
    return false
  }
}

/**
 * 记录用户手动选择的模型，供后续新建会话继承。
 * @param {string} spec 模型 spec；空值视为清除偏好
 * @param {Storage} [storage] 可注入的存储实现，默认使用全局 localStorage
 * @returns {boolean} 是否写入成功
 */
export const writeChatModelPreference = (spec, storage) => {
  const targetStorage = resolveStorage(storage)
  if (!targetStorage) return false
  if (!spec) return clearChatModelPreference(targetStorage)

  try {
    targetStorage.setItem(CHAT_MODEL_STORAGE_KEY, spec)
    return true
  } catch {
    return false
  }
}

/**
 * 解析聊天页当前模型。
 * @param {{ selectedModel?: string, conversationModel?: string, savedModel?: string }} params
 *   selectedModel 为本会话内的显式选择，conversationModel 为会话绑定（metadata.model_spec），
 *   savedModel 为用户本地偏好；新会话在前两者缺失时沿用偏好。
 */
export const resolveConversationModel = ({ selectedModel, conversationModel, savedModel }) =>
  selectedModel || conversationModel || savedModel || ''

/**
 * 从模型 spec 中取出用于展示的模型名。
 * spec 形如 `供应商:模型名`（模型名本身可含 `/`，如 `siliconflow-cn:deepseek-ai/DeepSeek-V4-Flash`），
 * 只切掉第一个冒号及其之前的供应商段，保留模型名中的斜杠。
 * @param {string} spec 模型 spec
 * @returns {string} 模型名；无冒号时原样返回
 */
export const extractModelName = (spec) => {
  if (!spec) return ''

  const separatorIndex = spec.indexOf(':')
  return separatorIndex >= 0 ? spec.slice(separatorIndex + 1) : spec
}

/**
 * 收集模型列表接口返回的全部 spec。
 * @param {Object} v2Models 以供应商 id 为键、值为 { models: [...] } 的对象
 * @returns {string[]} 全部模型 spec；结构异常时返回空数组
 */
const collectAvailableSpecs = (v2Models) => {
  if (!v2Models || typeof v2Models !== 'object') return []

  return Object.values(v2Models).flatMap((providerData) => {
    const models = providerData && providerData.models
    return Array.isArray(models) ? models.map((model) => model && model.spec).filter(Boolean) : []
  })
}

/**
 * 判断某个模型 spec 是否仍出现在模型列表接口的返回中。
 *
 * 用于识别「模型已被供应商下架」：此时页面仍会展示该模型名，但后端会在发送时
 * 以 422 拒绝请求，必须提前发现而不是让用户撞上错误。
 *
 * @param {Object} v2Models 以供应商 id 为键、值为 { models: [...] } 的对象
 * @param {string} spec 待校验的模型 spec
 * @returns {boolean} spec 非空且仍在可用列表中返回 true；spec 为空返回 false
 */
export const isModelSpecAvailable = (v2Models, spec) => {
  if (!spec) return false

  return collectAvailableSpecs(v2Models).includes(spec)
}

/**
 * 选择模型失效时的兜底 spec。
 *
 * 优先级：列表首项（与 ModelSelectorComponent 的兜底首项语义保持一致）→ 空字符串。
 * 返回空字符串表示列表里没有任何可用模型，调用方应保持原状而不是替换成非法值。
 *
 * @param {Object} v2Models 以供应商 id 为键、值为 { models: [...] } 的对象
 * @returns {string} 兜底模型 spec；无可用模型时为空字符串
 */
export const resolveFallbackModelSpec = (v2Models) =>
  collectAvailableSpecs(v2Models)[0] || ''
