"""初始化代码拥有的企业内置角色及功能权限。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from yuxi.modules.identity.models import Role, RolePermission
from yuxi.modules.identity.permissions.role_catalog import BUILTIN_ROLES
from yuxi.modules.identity.repositories.users import UserRepository


async def ensure_builtin_roles(db: AsyncSession) -> None:
    """在部署事务中幂等同步内置角色，保留自定义角色与用户分配。"""
    await UserRepository(db).lock_identity_changes()
    for definition in BUILTIN_ROLES:
        role = await db.scalar(select(Role).options(selectinload(Role.permissions)).where(Role.code == definition.code))
        if role is None:
            role = Role(code=definition.code, is_builtin=True, permissions=[], default_departments=[])
            db.add(role)
        elif not role.is_builtin:
            raise ValueError(f"内置角色标识被自定义角色占用: {definition.code}")
        role.name = definition.name
        role.description = definition.description
        role.is_active = True
        role.default_scope_type = definition.default_scope_type
        desired = set(definition.permission_keys)
        current = {permission.permission_key for permission in role.permissions}
        for permission in tuple(role.permissions):
            if permission.permission_key not in desired:
                await db.delete(permission)
        for key in definition.permission_keys:
            if key not in current:
                role.permissions.append(RolePermission(permission_key=key))
    await db.flush()
