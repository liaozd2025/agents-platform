let authRequiredHandler = null

/** 注册当前嵌入页的重新授权处理器，并返回清理函数。 */
export function setOAEmbedAuthRequiredHandler(handler) {
  authRequiredHandler = handler
  return () => {
    if (authRequiredHandler === handler) authRequiredHandler = null
  }
}

/** 请求 OA 重新授权；返回是否存在活动嵌入会话。 */
export function requestOAEmbedAuthentication() {
  if (!authRequiredHandler) return false
  authRequiredHandler()
  return true
}
