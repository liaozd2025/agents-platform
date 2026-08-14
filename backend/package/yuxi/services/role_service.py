"""角色与权限只读总览用例。"""

from dataclasses import asdict

from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.permissions.role_catalog import DATA_SCOPE_CATALOG, PERMISSION_CATALOG
from yuxi.repositories.role_repository import RoleRepository
from yuxi.storage.postgres.models_business import Role


def _serialize_role(role: Role) -> dict:
    """把角色及其只读详情转换为 API 数据。"""

    catalog_keys = [item.key for item in PERMISSION_CATALOG]
    assigned_keys = {item.permission_key for item in role.permissions}
    unknown_keys = assigned_keys.difference(catalog_keys)
    if unknown_keys:
        raise ValueError(f"角色 {role.code} 引用了未知功能权限: {', '.join(sorted(unknown_keys))}")

    members = [
        {
            "id": assignment.user.id,
            "uid": assignment.user.uid,
            "username": assignment.user.username,
        }
        for assignment in role.assignments
        if assignment.user is not None and assignment.user.is_deleted == 0
    ]
    members.sort(key=lambda member: member["id"])

    return {
        "id": role.id,
        "code": role.code,
        "name": role.name,
        "description": role.description,
        "is_builtin": bool(role.is_builtin),
        "is_active": bool(role.is_active),
        "default_scope_type": role.default_scope_type,
        "default_department_ids": sorted(item.department_id for item in role.default_departments),
        "permission_keys": [key for key in catalog_keys if key in assigned_keys],
        "member_count": len(members),
        "members": members,
    }


async def get_role_overview(db: AsyncSession) -> dict:
    """返回权限目录、数据范围目录和全部角色只读详情。"""

    roles = await RoleRepository(db).list_with_details()
    return {
        "permissions": [asdict(item) for item in PERMISSION_CATALOG],
        "data_scope_types": [asdict(item) for item in DATA_SCOPE_CATALOG],
        "roles": [_serialize_role(role) for role in roles],
    }
