/**
 * 按服务端权限目录顺序返回角色已拥有的功能权限分组。
 */
export function groupRolePermissions(catalog, permissionKeys) {
  const selectedKeys = new Set(permissionKeys)
  const groups = new Map()

  for (const permission of catalog) {
    if (!selectedKeys.has(permission.key)) continue

    if (!groups.has(permission.group)) {
      groups.set(permission.group, [])
    }
    groups.get(permission.group).push(permission)
  }

  return Array.from(groups, ([label, permissions]) => ({ label, permissions }))
}

/**
 * 从服务端数据范围目录中读取中文名称。
 */
export function getDataScopeLabel(scopeTypes, scopeType) {
  return scopeTypes.find((item) => item.key === scopeType)?.label || '未知范围'
}

/**
 * 返回安全审计动作的中文名称。
 */
export function getRoleAuditActionLabel(action) {
  return (
    {
      'role.create': '创建角色',
      'role.copy': '复制角色',
      'role.update': '修改角色',
      'role.deactivate': '停用角色'
    }[action] || action
  )
}
