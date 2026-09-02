export const DEFAULT_CHAT_MODEL = 'siliconflow-cn:deepseek-ai/DeepSeek-V4-Flash'

/**
 * 解析聊天页当前模型。已有明确选择优先，新会话固定使用可用的 DeepSeek，
 * 不读取异步加载的智能体或系统配置，避免默认模型回跳到已停用的 Qwen。
 */
export const resolveConversationModel = ({ selectedModel, conversationModel }) =>
  selectedModel || conversationModel || DEFAULT_CHAT_MODEL
