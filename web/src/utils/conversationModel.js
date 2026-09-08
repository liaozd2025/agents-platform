// 新建会话使用系统当前配置的 DashScope 模型；已有会话仍优先使用自身保存的模型。
export const DEFAULT_CHAT_MODEL = 'alibaba-cn:qwen3.7-max'

/**
 * 解析聊天页当前模型。已有明确选择或会话绑定优先，新会话使用 DashScope 默认模型。
 * 保持历史会话模型不变，避免切换系统默认模型时篡改既有会话行为。
 */
export const resolveConversationModel = ({ selectedModel, conversationModel }) =>
  selectedModel || conversationModel || DEFAULT_CHAT_MODEL
