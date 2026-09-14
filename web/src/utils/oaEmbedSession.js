/**
 * 嵌入会话的跨层通道。
 *
 * 嵌入桥实例持有在 `useOAEmbedBridge` 中，而来源列表组件位于深层子组件，
 * 两者之间相隔 AppLayout 等多层，因此这里用模块级 handler 做一次性注册，
 * 供不继承 props 的组件直接请求父页面动作。
 */

let authRequiredHandler = null
let navigateHandler = null

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

/** 注册当前嵌入页的父页面导航处理器，并返回清理函数。 */
export function setOAEmbedNavigateHandler(handler) {
  navigateHandler = handler
  return () => {
    if (navigateHandler === handler) navigateHandler = null
  }
}

/** 请求父页面内跳转；返回是否存在活动嵌入会话（false 表示应由调用方降级处理）。 */
export function requestOAEmbedNavigate({ taskId, ecType } = {}) {
  if (!navigateHandler) return false
  return navigateHandler({ taskId, ecType }) === true
}
