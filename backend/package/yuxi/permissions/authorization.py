"""请求级功能权限与组织数据范围判定。"""

from dataclasses import dataclass
from typing import Any

from yuxi.permissions.role_catalog import ALL_PERMISSION_KEYS


def parse_department_ancestor_ids(path: str | None) -> tuple[int, ...]:
    """从组织节点物化路径解析包含自身的祖先节点 ID。"""

    if not path:
        return ()
    return tuple(int(node_id) for node_id in path.strip("/").split("/") if node_id)


@dataclass(frozen=True)
class AuthorizationTarget:
    """一次数据访问的明确所有者和组织祖先链。"""

    owner_user_id: int | None = None
    department_ancestor_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class _EffectiveAssignment:
    """一条角色分配解析后的权限与数据范围。"""

    permission_keys: frozenset[str]
    scope_type: str
    department_ids: frozenset[int]


@dataclass(frozen=True)
class AuthorizationContext:
    """当前请求中可复用的用户有效授权快照。"""

    user: Any
    assignments: tuple[_EffectiveAssignment, ...]
    effective_permissions: tuple[str, ...]

    def has_permission(self, permission_key: str) -> bool:
        """判断任一有效分配是否授予功能权限。"""

        return permission_key in self.effective_permissions

    def allows(self, permission_key: str, target: AuthorizationTarget | None = None) -> bool:
        """在同一条角色分配内同时判断功能权限和目标数据范围。"""

        return any(
            permission_key in assignment.permission_keys and (target is None or self._scope_matches(assignment, target))
            for assignment in self.assignments
        )

    def _scope_matches(self, assignment: _EffectiveAssignment, target: AuthorizationTarget) -> bool:
        """判断一条分配的数据范围是否覆盖目标。"""

        scope_type = assignment.scope_type
        if scope_type == "all":
            return True
        if scope_type == "self":
            return target.owner_user_id == self.user.id
        if scope_type == "organization_and_descendants":
            return self.user.department_id is not None and self.user.department_id in target.department_ancestor_ids
        if scope_type == "selected_organizations_and_descendants":
            return not assignment.department_ids.isdisjoint(target.department_ancestor_ids)
        return False


def build_authorization_context(user: Any) -> AuthorizationContext:
    """从已加载的用户角色关系生成默认拒绝的授权上下文。"""

    catalog_keys = set(ALL_PERMISSION_KEYS)
    assignments = []
    for assignment in user.role_assignments:
        role = assignment.role
        if not role.is_active:
            continue

        inherited = assignment.scope_mode == "inherit"
        scope_type = role.default_scope_type if inherited else assignment.override_scope_type
        scope_departments = role.default_departments if inherited else assignment.scope_departments
        assignments.append(
            _EffectiveAssignment(
                permission_keys=frozenset(
                    item.permission_key for item in role.permissions if item.permission_key in catalog_keys
                ),
                scope_type=scope_type or "none",
                department_ids=frozenset(item.department_id for item in scope_departments),
            )
        )

    effective_keys = {key for assignment in assignments for key in assignment.permission_keys}
    return AuthorizationContext(
        user=user,
        assignments=tuple(assignments),
        effective_permissions=tuple(key for key in ALL_PERMISSION_KEYS if key in effective_keys),
    )
