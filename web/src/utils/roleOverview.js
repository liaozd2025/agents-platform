import { isDepartmentSelectionCovered } from './departmentTree.js'

/**
 * 将角色成员转换为可直接下载的 CSV 内容。
 */
export function serializeRoleMembersCsv(members) {
  /** 转义单个 CSV 字段。 */
  const escapeCell = (value) => {
    const text = String(value)
    return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text
  }

  return [
    ['成员', '账号'],
    ...members.map((member) => [member.username, member.uid])
  ]
    .map((row) => row.map(escapeCell).join(','))
    .join('\n')
}

/**
 * 按服务端权限目录组成菜单与操作权限层级。
 */
export function groupRolePermissions(catalog, permissionKeys, onlyGranted = false) {
  const selectedKeys = new Set(permissionKeys)
  const menus = new Map()
  const groups = new Map()

  const menuPermissions = catalog
    .filter((permission) => !permission.parent_key)
    .sort((left, right) => (left.display_order || 0) - (right.display_order || 0))

  for (const permission of menuPermissions) {

    if (!groups.has(permission.group)) {
      groups.set(permission.group, [])
    }

    const menu = {
      ...permission,
      granted: selectedKeys.has(permission.key),
      operations: []
    }
    menus.set(permission.key, menu)
    groups.get(permission.group).push(menu)
  }

  for (const permission of catalog) {
    if (!permission.parent_key) continue

    const menu = menus.get(permission.parent_key)
    if (!menu) throw new Error(`操作权限 ${permission.key} 缺少菜单权限 ${permission.parent_key}`)
    menu.operations.push({ ...permission, granted: selectedKeys.has(permission.key) })
  }

  return Array.from(groups, ([label, groupMenus]) => {
    const visibleMenus = groupMenus
      .filter(
        (menu) =>
          !onlyGranted || menu.granted || menu.operations.some((operation) => operation.granted)
      )
      .map((menu) => ({
        ...menu,
        granted_operation_count: menu.operations.filter((operation) => operation.granted).length,
        operation_count: menu.operations.length,
        operations: onlyGranted
          ? menu.operations.filter((operation) => operation.granted)
          : menu.operations
      }))

    return { label, menus: visibleMenus }
  }).filter((group) => group.menus.length)
}

/**
 * 更新一个权限并保持菜单与操作权限的父子联动。
 */
export function updateRolePermissionSelection(permissionKeys, catalog, permissionKey, checked) {
  const target = catalog.find((permission) => permission.key === permissionKey)
  if (!target) throw new Error(`权限目录中不存在 ${permissionKey}`)

  const selectedKeys = new Set(permissionKeys)
  if (checked) {
    selectedKeys.add(permissionKey)
    if (target.parent_key) selectedKeys.add(target.parent_key)
  } else {
    selectedKeys.delete(permissionKey)
    if (!target.parent_key) {
      for (const permission of catalog) {
        if (permission.parent_key === permissionKey) selectedKeys.delete(permission.key)
      }
    }
  }

  return catalog
    .map((permission) => permission.key)
    .filter((key) => selectedKeys.has(key))
}

/**
 * 从服务端数据范围目录中读取中文名称。
 */
export function getDataScopeLabel(scopeTypes, scopeType) {
  return scopeTypes.find((item) => item.key === scopeType)?.label || '未知范围'
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
