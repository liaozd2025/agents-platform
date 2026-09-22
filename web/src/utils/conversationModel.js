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
