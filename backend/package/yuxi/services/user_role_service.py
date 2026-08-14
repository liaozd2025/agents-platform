"""用户多角色分配、范围收窄与超级管理员保护。"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.permissions.role_catalog import DATA_SCOPE_CATALOG
from yuxi.repositories.department_repository import DepartmentRepository
from yuxi.repositories.role_repository import RoleRepository
from yuxi.storage.postgres.models_business import (
    SecurityAudit,
    User,
    UserRoleAssignment,
    UserRoleAssignmentDepartment,
)


class UserRoleConflictError(ValueError):
    """用户角色状态违反必须保留的系统约束。"""


def serialize_user_roles(user: User) -> list[dict[str, Any]]:
    """返回用户管理与当前用户接口使用的角色分配数组。"""

    roles = []
    for assignment in sorted(user.role_assignments, key=lambda item: item.role.id):
        role = assignment.role
        default_department_ids = sorted(item.department_id for item in role.default_departments)
        override_department_ids = sorted(item.department_id for item in assignment.scope_departments)
        inherited = assignment.scope_mode == "inherit"
        effective_scope_type = role.default_scope_type if inherited else assignment.override_scope_type
        effective_department_ids = default_department_ids if inherited else override_department_ids
        roles.append(
            {
                "assignment_id": assignment.id,
                "id": role.id,
                "code": role.code,
                "name": role.name,
                "is_builtin": bool(role.is_builtin),
                "is_active": bool(role.is_active),
                "scope_mode": assignment.scope_mode,
                "default_scope_type": role.default_scope_type,
                "default_department_ids": default_department_ids,
                "override_scope_type": assignment.override_scope_type,
                "override_department_ids": override_department_ids,
                "effective_scope_type": effective_scope_type,
                "effective_department_ids": effective_department_ids,
            }
        )
    return roles


def serialize_user(user: User, department_name: str | None = None) -> dict[str, Any]:
    """序列化用户基本信息和多角色分配，迁移期保留旧角色字段。"""

    return {**user.to_dict(), "department_name": department_name, "roles": serialize_user_roles(user)}


def _assignment_snapshot(user: User) -> dict[str, Any]:
    """生成不依赖分配记录主键的稳定审计快照。"""

    roles = [
        {
            "role_id": item["id"],
            "code": item["code"],
            "scope_mode": item["scope_mode"],
            "override_scope_type": item["override_scope_type"],
            "override_department_ids": item["override_department_ids"],
        }
        for item in serialize_user_roles(user)
    ]
    return {"roles": roles}


def _scope_is_within_default(
    *,
    default_scope_type: str,
    default_department_ids: list[int],
    override_scope_type: str,
    override_department_ids: list[int],
    user_department_id: int | None,
    paths: dict[int, str],
) -> bool:
    """判断个性化范围是否为角色默认范围的子集。"""

    if override_scope_type == "none" or default_scope_type == "all":
        return True
    if default_scope_type == "none":
        return False
    if default_scope_type == "self":
        return override_scope_type == "self"

    user_path = paths.get(user_department_id) if user_department_id is not None else None
    if default_scope_type == "organization_and_descendants":
        if override_scope_type == "self":
            return True
        if override_scope_type == "organization_and_descendants":
            return True
        if override_scope_type == "selected_organizations_and_descendants" and user_path:
            return all(paths[department_id].startswith(user_path) for department_id in override_department_ids)
        return False

    default_paths = [paths[department_id] for department_id in default_department_ids]
    if override_scope_type in {"self", "organization_and_descendants"}:
        return bool(user_path and any(user_path.startswith(path) for path in default_paths))
    if override_scope_type == "selected_organizations_and_descendants":
        return all(
            any(paths[department_id].startswith(default_path) for default_path in default_paths)
            for department_id in override_department_ids
        )
    return False


async def replace_user_role_assignments(
    db: AsyncSession,
    *,
    actor: User,
    target: User,
    assignments: list[dict[str, Any]],
    reason: str | None = None,
) -> None:
    """完整替换有效用户的角色分配，并在同一事务中记录安全审计。"""

    if not assignments:
        raise ValueError("每个有效用户至少需要一个角色")

    role_ids = [int(item["role_id"]) for item in assignments]
    if len(role_ids) != len(set(role_ids)):
        raise ValueError("同一角色不能重复分配")

    role_repo = RoleRepository(db)
    roles = await role_repo.lock_assignment_roles(role_ids)
    role_by_id = {role.id: role for role in roles}
    if len(role_by_id) != len(role_ids):
        raise ValueError("角色不存在")
    if any(not role.is_active for role in roles):
        raise ValueError("不能分配已停用角色")

    assigned_codes = {role.code for role in roles}
    has_superadmin = "superadmin" in assigned_codes
    if has_superadmin and len(roles) != 1:
        raise ValueError("superadmin 不能与其他角色同时分配")

    before = _assignment_snapshot(target)
    had_superadmin = any(item["code"] == "superadmin" for item in before["roles"])
    normalized_reason = (reason or "").strip()
    if had_superadmin != has_superadmin:
        if actor.role != "superadmin":
            raise ValueError("只有超级管理员可以授予或撤销 superadmin")
        if not normalized_reason:
            raise ValueError("授予或撤销 superadmin 必须填写原因")

        active_superadmin_count = await role_repo.lock_superadmin_and_count_active_users()
        if had_superadmin and target.is_deleted == 0 and active_superadmin_count <= 1:
            raise UserRoleConflictError("系统必须至少保留一个有效超级管理员")

    normalized_assignments = []
    department_ids = set()
    valid_scope_types = {item.key for item in DATA_SCOPE_CATALOG}
    for item in assignments:
        role = role_by_id[int(item["role_id"])]
        scope_mode = item.get("scope_mode", "inherit")
        override_scope_type = item.get("override_scope_type")
        override_department_ids = sorted({int(value) for value in item.get("override_department_ids", [])})

        if scope_mode == "inherit":
            if override_scope_type is not None or override_department_ids:
                raise ValueError("继承角色默认范围时不能保存个性化范围")
        elif scope_mode == "override":
            if override_scope_type not in valid_scope_types:
                raise ValueError("未知个性化数据范围")
            selected_scope = override_scope_type == "selected_organizations_and_descendants"
            if selected_scope and not override_department_ids:
                raise ValueError("指定组织及下级范围至少需要选择一个组织节点")
            if not selected_scope and override_department_ids:
                raise ValueError("只有指定组织及下级范围可以保存组织节点")
        else:
            raise ValueError("未知角色分配范围模式")

        department_ids.update(override_department_ids)
        department_ids.update(item.department_id for item in role.default_departments)
        normalized_assignments.append(
            {
                "role": role,
                "scope_mode": scope_mode,
                "override_scope_type": override_scope_type,
                "override_department_ids": override_department_ids,
            }
        )

    if target.department_id is not None:
        department_ids.add(target.department_id)
    paths = await DepartmentRepository().get_paths_by_ids(department_ids, session=db)
    if len(paths) != len(department_ids):
        raise ValueError("角色范围引用了不存在或已删除的组织节点")

    for item in normalized_assignments:
        override_department_ids = item["override_department_ids"]
        for index, department_id in enumerate(override_department_ids):
            path = paths[department_id]
            if any(
                path.startswith(paths[other_id]) or paths[other_id].startswith(path)
                for other_id in override_department_ids[index + 1 :]
            ):
                raise ValueError("同一数据范围不能同时选择上级和下级组织节点")

        if item["scope_mode"] == "override" and not _scope_is_within_default(
            default_scope_type=item["role"].default_scope_type,
            default_department_ids=[entry.department_id for entry in item["role"].default_departments],
            override_scope_type=item["override_scope_type"],
            override_department_ids=override_department_ids,
            user_department_id=target.department_id,
            paths=paths,
        ):
            raise ValueError("个性化覆盖范围不能超过角色默认范围")

    existing_by_role_id = {assignment.role.id: assignment for assignment in target.role_assignments}
    next_assignments = []
    for item in normalized_assignments:
        role = item["role"]
        assignment = existing_by_role_id.pop(role.id, None) or UserRoleAssignment(user=target, role=role)
        assignment.scope_mode = item["scope_mode"]
        assignment.override_scope_type = item["override_scope_type"] if item["scope_mode"] == "override" else None
        assignment.scope_departments = [
            UserRoleAssignmentDepartment(department_id=department_id)
            for department_id in item["override_department_ids"]
        ]
        next_assignments.append(assignment)
    target.role_assignments = next_assignments

    target.role = "superadmin" if has_superadmin else "admin" if "admin" in assigned_codes else "user"
    after = _assignment_snapshot(target)
    if before != after:
        db.add(
            SecurityAudit(
                actor_user_id=actor.id,
                action="user.roles.update",
                target_type="user",
                target_id=target.id,
                target_code=target.uid,
                reason=normalized_reason or None,
                before_value=before,
                after_value=after,
            )
        )
    await db.flush()
