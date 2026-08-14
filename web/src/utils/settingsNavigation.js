const SETTINGS_NAVIGATION_GROUPS = [
  {
    label: '个人',
    items: [
      { id: 'account', label: '账户设置', access: 'loggedIn' },
      { id: 'agentEnv', label: '环境变量', access: 'loggedIn' }
    ]
  },
  {
    label: '系统',
    items: [
      { id: 'base', label: '基本设置', access: 'systemConfig' },
      { id: 'user', label: '用户管理', access: 'userRead' },
      { id: 'department', label: '组织机构', access: 'departmentRead' },
      { id: 'role', label: '角色与权限', access: 'roleRead' }
    ]
  },
  {
    label: '平台能力',
    items: [
      { id: 'apiKeys', label: 'API Keys', access: 'loggedIn' },
      { id: 'ocr', label: 'OCR 配置', access: 'ocrManage' }
    ]
  }
]

/**
 * 按当前用户权限和搜索词返回可见的设置导航分组。
 */
export function getSettingsNavigationGroups(permissions, searchQuery = '') {
  const access = {
    loggedIn: permissions.isLoggedIn,
    systemConfig: permissions.effectivePermissions.includes('system_config:manage'),
    ocrManage: permissions.effectivePermissions.includes('ocr:manage'),
    userRead: permissions.effectivePermissions.includes('user:read'),
    roleRead: permissions.effectivePermissions.includes('role:read'),
    departmentRead: ['department:read', 'department:read_all'].some((permission) =>
      permissions.effectivePermissions.includes(permission)
    )
  }
  const query = searchQuery.trim().toLowerCase()

  return SETTINGS_NAVIGATION_GROUPS.map((group) => ({
    label: group.label,
    items: group.items
      .filter((item) => access[item.access])
      .filter(
        (item) =>
          !query ||
          item.label.toLowerCase().includes(query) ||
          item.id.toLowerCase().includes(query)
      )
      .map(({ id, label }) => ({ id, label }))
  })).filter((group) => group.items.length)
}
