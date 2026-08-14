"""自定义角色生命周期与结构化安全审计用例。"""

from dataclasses import asdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.permissions.authorization import (
    AuthorizationContext,
    AuthorizationTarget,
    parse_department_ancestor_ids,
)
from yuxi.permissions.role_catalog import DATA_SCOPE_CATALOG, PERMISSION_CATALOG
from yuxi.repositories.department_repository import DepartmentRepository
from yuxi.repositories.role_repository import RoleRepository
from yuxi.services.user_role_service import get_assignable_role_constraints
from yuxi.storage.postgres.models_business import (
    Role,
    RoleDefaultDepartment,
    RolePermission,
    SecurityAudit,
    User,
)
from yuxi.utils.datetime_utils import format_utc_datetime


class RoleNotFoundError(ValueError):
    """请求的角色不存在。"""


class RoleConflictError(ValueError):
    """角色当前状态不允许执行请求的操作。"""


def _role_snapshot(role: Role) -> dict[str, Any]:
    """生成安全审计使用的稳定角色定义快照。"""

    catalog_keys = [item.key for item in PERMISSION_CATALOG]
    assigned_keys = {item.permission_key for item in role.permissions}
    return {
        "code": role.code,
        "name": role.name,
        "description": role.description,
        "is_active": bool(role.is_active),
        "default_scope_type": role.default_scope_type,
        "default_department_ids": sorted(item.department_id for item in role.default_departments),
        "permission_keys": [key for key in catalog_keys if key in assigned_keys],
    }


def _serialize_audit(audit: SecurityAudit) -> dict[str, Any]:
    """将安全审计转换为角色详情可展示的数据。"""

    actor = audit.actor
    return {
        "id": audit.id,
        "action": audit.action,
        "actor": {
            "id": actor.id,
            "uid": actor.uid,
            "username": actor.username,
        },
        "target": {
            "type": audit.target_type,
            "id": audit.target_id,
            "code": audit.target_code,
        },
        "reason": audit.reason,
        "before": audit.before_value,
        "after": audit.after_value,
        "created_at": format_utc_datetime(audit.created_at),
    }


def _serialize_role(
    role: Role,
    audits: list[SecurityAudit],
    visible_user_ids: set[int] | None = None,
) -> dict[str, Any]:
    """把角色、成员和相关审计转换为 API 数据。"""

    snapshot = _role_snapshot(role)
    assigned_keys = {item.permission_key for item in role.permissions}
    unknown_keys = assigned_keys.difference(item.key for item in PERMISSION_CATALOG)
    if unknown_keys:
        raise ValueError(f"角色 {role.code} 引用了未知功能权限: {', '.join(sorted(unknown_keys))}")

    members = [
        {
            "id": assignment.user.id,
            "uid": assignment.user.uid,
            "username": assignment.user.username,
        }
        for assignment in role.assignments
        if assignment.user is not None
        and assignment.user.is_deleted == 0
        and (visible_user_ids is None or assignment.user.id in visible_user_ids)
    ]
    members.sort(key=lambda member: member["id"])

    return {
        "id": role.id,
        **snapshot,
        "is_builtin": bool(role.is_builtin),
        "member_count": len(members),
        "members": members,
        "audits": [
            _serialize_audit(audit)
            for audit in audits
            if visible_user_ids is None or audit.actor_user_id in visible_user_ids
        ],
    }


async def _validate_role_definition(
    db: AsyncSession,
    *,
    name: str,
    description: str,
    permission_keys: list[str],
    default_scope_type: str,
    default_department_ids: list[int],
) -> dict[str, Any]:
    """校验目录标识和指定组织子树，并返回规范化角色定义。"""

    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("角色名称不能为空")

    catalog_keys = [item.key for item in PERMISSION_CATALOG]
    selected_keys = set(permission_keys)
    unknown_keys = selected_keys.difference(catalog_keys)
    if unknown_keys:
        raise ValueError(f"未知功能权限: {', '.join(sorted(unknown_keys))}")

    scope_keys = {item.key for item in DATA_SCOPE_CATALOG}
    if default_scope_type not in scope_keys:
        raise ValueError("未知默认数据范围")

    department_ids = sorted({int(department_id) for department_id in default_department_ids})
    selected_scope = default_scope_type == "selected_organizations_and_descendants"
    if selected_scope and not department_ids:
        raise ValueError("指定组织及下级范围至少需要选择一个组织节点")
    if not selected_scope and department_ids:
        raise ValueError("只有指定组织及下级范围可以保存组织节点")

    if department_ids:
        paths = await DepartmentRepository().get_paths_by_ids(department_ids, session=db)
        if len(paths) != len(department_ids):
            raise ValueError("所选组织节点不存在或已删除")
        for index, department_id in enumerate(department_ids):
            path = paths[department_id]
            if any(
                path.startswith(paths[other_id]) or paths[other_id].startswith(path)
                for other_id in department_ids[index + 1 :]
            ):
                raise ValueError("同一数据范围不能同时选择上级和下级组织节点")

    return {
        "name": normalized_name,
        "description": description.strip(),
        "permission_keys": [key for key in catalog_keys if key in selected_keys],
        "default_scope_type": default_scope_type,
        "default_department_ids": department_ids,
    }


def _apply_definition(role: Role, definition: dict[str, Any]) -> None:
    """用已校验定义完整替换一个自定义角色。"""

    role.name = definition["name"]
    role.description = definition["description"]
    role.default_scope_type = definition["default_scope_type"]
    role.permissions = [RolePermission(permission_key=key) for key in definition["permission_keys"]]
    role.default_departments = [
        RoleDefaultDepartment(department_id=department_id) for department_id in definition["default_department_ids"]
    ]


def _add_audit(
    db: AsyncSession,
    *,
    actor: User,
    action: str,
    role: Role,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> None:
    """在角色变更所在事务中追加一条结构化安全审计。"""

    db.add(
        SecurityAudit(
            actor_user_id=actor.id,
            action=action,
            target_type="role",
            target_id=role.id,
            target_code=role.code,
            before_value=before,
            after_value=after,
        )
    )


async def _get_serialized_role(db: AsyncSession, role_id: int) -> dict[str, Any]:
    """重新读取并返回一次角色完整详情。"""

    repository = RoleRepository(db)
    role = await repository.get_with_details(role_id)
    if role is None:
        raise RoleNotFoundError("角色不存在")
    audits = await repository.list_role_audits([role_id])
    return _serialize_role(role, audits[role_id])


async def get_role_overview(
    db: AsyncSession,
    authorization: AuthorizationContext,
    assignment_target: User | None = None,
) -> dict[str, Any]:
    """返回角色定义以及当前数据范围内的成员和审计。"""

    repository = RoleRepository(db)
    department_repository = DepartmentRepository()
    roles = await repository.list_with_details()
    audits = await repository.list_role_audits([role.id for role in roles])

    users = {
        assignment.user.id: assignment.user
        for role in roles
        for assignment in role.assignments
        if assignment.user is not None and assignment.user.is_deleted == 0
    }
    for role_audits in audits.values():
        for audit in role_audits:
            users[audit.actor.id] = audit.actor

    department_ids = {user.department_id for user in users.values() if user.department_id is not None}
    department_paths = await department_repository.get_paths_by_ids(department_ids, session=db)
    visible_user_ids = {
        user.id
        for user in users.values()
        if authorization.allows(
            "role:read",
            AuthorizationTarget(
                owner_user_id=user.id,
                department_ancestor_ids=parse_department_ancestor_ids(department_paths.get(user.department_id)),
            ),
        )
    }

    scope_department_ids = {item.department_id for role in roles for item in role.default_departments}
    scope_department_names = await department_repository.get_names_by_ids(scope_department_ids, session=db)

    serialized_roles = [_serialize_role(role, audits[role.id], visible_user_ids) for role in roles]
    if assignment_target is not None:
        constraints = await get_assignable_role_constraints(authorization, assignment_target, roles)
        serialized_roles = [
            {**role, "assignment_constraints": constraints[role["id"]]}
            for role in serialized_roles
            if role["id"] in constraints
        ]

    return {
        "permissions": [asdict(item) for item in PERMISSION_CATALOG],
        "data_scope_types": [asdict(item) for item in DATA_SCOPE_CATALOG],
        "scope_departments": [
            {"id": department_id, "name": name} for department_id, name in scope_department_names.items()
        ],
        "roles": serialized_roles,
    }


async def create_custom_role(
    db: AsyncSession,
    actor: User,
    *,
    code: str,
    name: str,
    description: str,
    permission_keys: list[str],
    default_scope_type: str,
    default_department_ids: list[int],
    audit_action: str = "role.create",
) -> dict[str, Any]:
    """创建一个独立自定义角色并记录安全审计。"""

    repository = RoleRepository(db)
    if await repository.get_by_code(code) is not None:
        raise RoleConflictError("角色标识已存在")

    definition = await _validate_role_definition(
        db,
        name=name,
        description=description,
        permission_keys=permission_keys,
        default_scope_type=default_scope_type,
        default_department_ids=default_department_ids,
    )
    role = Role(code=code, is_builtin=False, is_active=True, default_scope_type=definition["default_scope_type"])
    _apply_definition(role, definition)
    db.add(role)
    await db.flush()

    _add_audit(db, actor=actor, action=audit_action, role=role, before=None, after=_role_snapshot(role))
    await db.flush()
    return await _get_serialized_role(db, role.id)


async def copy_role(
    db: AsyncSession,
    actor: User,
    role_id: int,
    *,
    code: str,
    name: str,
    description: str | None,
) -> dict[str, Any]:
    """复制已有角色为授权完全独立的自定义角色。"""

    source = await RoleRepository(db).get_with_details(role_id)
    if source is None:
        raise RoleNotFoundError("角色不存在")
    snapshot = _role_snapshot(source)
    return await create_custom_role(
        db,
        actor,
        code=code,
        name=name,
        description=snapshot["description"] if description is None else description,
        permission_keys=snapshot["permission_keys"],
        default_scope_type=snapshot["default_scope_type"],
        default_department_ids=snapshot["default_department_ids"],
        audit_action="role.copy",
    )


async def update_custom_role(
    db: AsyncSession,
    actor: User,
    role_id: int,
    *,
    name: str,
    description: str,
    permission_keys: list[str],
    default_scope_type: str,
    default_department_ids: list[int],
) -> dict[str, Any]:
    """完整更新自定义角色定义并记录变更前后值。"""

    role = await RoleRepository(db).get_with_details(role_id)
    if role is None:
        raise RoleNotFoundError("角色不存在")
    if role.is_builtin:
        raise RoleConflictError("内置角色不能修改")

    definition = await _validate_role_definition(
        db,
        name=name,
        description=description,
        permission_keys=permission_keys,
        default_scope_type=default_scope_type,
        default_department_ids=default_department_ids,
    )
    before = _role_snapshot(role)
    _apply_definition(role, definition)
    after = _role_snapshot(role)
    if before != after:
        _add_audit(db, actor=actor, action="role.update", role=role, before=before, after=after)

    await db.flush()
    return await _get_serialized_role(db, role.id)


async def deactivate_custom_role(db: AsyncSession, actor: User, role_id: int) -> dict[str, Any]:
    """停用没有有效成员的自定义角色。"""

    repository = RoleRepository(db)
    role = await repository.get_with_details(role_id)
    if role is None:
        raise RoleNotFoundError("角色不存在")
    if role.is_builtin:
        raise RoleConflictError("内置角色不能停用")
    if not role.is_active:
        return await _get_serialized_role(db, role.id)
    if await repository.count_active_members(role.id):
        raise RoleConflictError("该角色仍有成员，请先迁移成员后再停用")

    before = _role_snapshot(role)
    role.is_active = False
    _add_audit(db, actor=actor, action="role.deactivate", role=role, before=before, after=_role_snapshot(role))
    await db.flush()
    return await _get_serialized_role(db, role.id)
