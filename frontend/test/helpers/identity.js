/** 显式装配测试身份的角色与有效授权，匹配认证 API 的当前契约。 */
export function setTestUserRole(user, role) {
  user.userRoles = role === 'user' ? [] : [{ code: role, is_active: true }]
  user.effectivePermissions = ['agent:use', 'skill:use', 'knowledge_base:read']
  if (role === 'admin' || role === 'superadmin') user.effectivePermissions.push(
    'agent:manage', 'skill:manage', 'knowledge_base:manage', 'knowledge_evaluation:manage',
    'system_task:manage', 'model_provider:manage', 'dashboard:view', 'tool:manage',
    'user:manage', 'department:manage', 'api_key:manage'
  )
  if (role === 'superadmin') user.effectivePermissions.push(
    'mcp:manage', 'system_config:manage', 'ocr:manage', 'role:manage', 'system_log:read', 'user:impersonate'
  )
}
