const SETTINGS_NAVIGATION_GROUPS = [
  {
    label: '个人',
    items: [
      {
        id: 'account',
        label: '账户设置',
        access: 'loggedIn',
        path: '/settings/account',
        routeName: 'SettingsAccount'
      },
      {
        id: 'agentEnv',
        label: '环境变量',
        access: 'loggedIn',
        path: '/settings/agent-env',
        routeName: 'SettingsAgentEnv'
      }
    ]
  },
  {
    label: '系统',
    items: [
      {
        id: 'base',
        label: '基本设置',
        access: 'systemConfig',
        path: '/settings/base',
        routeName: 'SettingsBase',
        requiredPermission: 'system_config:manage'
      },
      {
        id: 'user',
        label: '用户管理',
        access: 'userRead',
        path: '/settings/user',
        routeName: 'SettingsUser',
        requiredPermission: 'user:read'
      },
      {
        id: 'department',
        label: '组织机构',
        access: 'departmentRead',
        path: '/settings/department',
        routeName: 'SettingsDepartment',
        requiredAnyPermissions: ['department:read', 'department:read_all']
      },
      {
        id: 'role',
        label: '角色与权限',
        access: 'roleRead',
        path: '/settings/role',
        routeName: 'SettingsRole',
        requiredPermission: 'role:read'
      }
    ]
  },
  {
    label: '平台能力',
    items: [
      {
        id: 'apiKeys',
        label: 'API Keys',
        access: 'loggedIn',
        path: '/settings/api-keys',
        routeName: 'SettingsApiKeys'
      },
      {
        id: 'ocr',
        label: 'OCR 配置',
        access: 'ocrManage',
        path: '/settings/ocr',
        routeName: 'SettingsOcr',
        requiredPermission: 'ocr:manage'
      }
    ]
  }
]

export const SETTINGS_ROUTES = SETTINGS_NAVIGATION_GROUPS.flatMap((group) => group.items)

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
      .map(({ id, label, path }) => ({ id, label, path }))
  })).filter((group) => group.items.length)
}
