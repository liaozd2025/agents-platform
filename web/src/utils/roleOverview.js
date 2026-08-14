import { isDepartmentSelectionCovered } from './departmentTree.js'

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

/**
 * 返回角色默认范围允许保存的个性化范围。
 */
export function getAssignableScopeTypes(
  defaultScopeType,
  scopeTypes,
  departments = [],
  targetDepartmentId = null,
  defaultDepartmentIds = [],
  constraints = null
) {
  let allowedScopes = {
    none: ['none'],
    self: ['none', 'self'],
    organization_and_descendants: [
      'none',
      'self',
      'organization_and_descendants',
      'selected_organizations_and_descendants'
    ],
    selected_organizations_and_descendants: [
      'none',
      'self',
      'organization_and_descendants',
      'selected_organizations_and_descendants'
    ],
    all: scopeTypes.map((scope) => scope.key)
  }[defaultScopeType]

  if (defaultScopeType === 'organization_and_descendants' && targetDepartmentId == null) {
    allowedScopes = allowedScopes.filter(
      (scope) => scope !== 'selected_organizations_and_descendants'
    )
  }
  if (
    defaultScopeType === 'selected_organizations_and_descendants' &&
    !isDepartmentSelectionCovered(departments, defaultDepartmentIds, [targetDepartmentId])
  ) {
    allowedScopes = allowedScopes.filter(
      (scope) => scope !== 'self' && scope !== 'organization_and_descendants'
    )
  }

  const constrainedScopes = constraints ? new Set(constraints.override_scope_types) : null
  return scopeTypes.filter(
    (scope) => allowedScopes?.includes(scope.key) && (!constrainedScopes || constrainedScopes.has(scope.key))
  )
}

/**
 * 切换角色时清空旧范围；选择超级管理员时移除其他角色。
 */
export function resetRoleAssignmentScope(assignments, index, isSuperadmin) {
  const changed = {
    ...assignments[index],
    scope_mode: 'inherit',
    override_scope_type: null,
    override_department_ids: []
  }
  return isSuperadmin
    ? [changed]
    : assignments.map((item, itemIndex) => (itemIndex === index ? changed : item))
}
