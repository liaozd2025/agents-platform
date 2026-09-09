/**
 * 解析聊天页当前模型。手动选择和已有会话绑定优先；新会话留空，
 * 由模型选择器从后端返回的已配置模型列表中选择首项。
 */
export const resolveConversationModel = ({ selectedModel, conversationModel }) =>
  selectedModel || conversationModel || ''
