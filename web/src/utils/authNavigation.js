/** 返回当前用户登录后可进入的默认后台路由。 */
export function getAuthenticatedHomePath(hasPermission) {
  if (hasPermission('agent:use')) return '/agent'
  if (hasPermission('dashboard:view')) return '/dashboard'
  return '/settings/account'
}
