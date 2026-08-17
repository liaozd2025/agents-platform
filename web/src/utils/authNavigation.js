/** 返回当前用户登录后可进入的默认后台路由。 */
export function getAuthenticatedHomePath(hasPermission) {
  if (hasPermission('agent:use')) return '/agent'
  if (hasPermission('dashboard:view')) return '/dashboard'
  return '/settings/account'
}

/** 判断当前用户是否满足目标路由声明的全部权限约束。 */
export function canAccessRoute(matchedRoutes, hasPermission) {
  const requiredPermissions = matchedRoutes
    .map((record) => record.meta.requiredPermission)
    .filter(Boolean)
  const requiredAnyPermissions = matchedRoutes.flatMap(
    (record) => record.meta.requiredAnyPermissions || []
  )

  return (
    requiredPermissions.every(hasPermission) &&
    (!requiredAnyPermissions.length || requiredAnyPermissions.some(hasPermission))
  )
}
