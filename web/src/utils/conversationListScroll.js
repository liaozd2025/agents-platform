/**
 * 判断会话列表是否应加载下一页。
 * 触底判断统一放在这里，避免滚动事件因加载中或已无更多数据而重复发起请求。
 */
export const shouldLoadMoreConversations = ({
  list,
  isLoadingMore,
  hasMoreChats,
  threshold = 48
}) => {
  if (!list || isLoadingMore || !hasMoreChats) return false
  const distanceToBottom = list.scrollHeight - list.scrollTop - list.clientHeight
  return distanceToBottom <= threshold
}
