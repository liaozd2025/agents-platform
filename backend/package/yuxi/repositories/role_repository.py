"""角色只读数据访问。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from yuxi.storage.postgres.models_business import Role, UserRoleAssignment


class RoleRepository:
    """读取角色总览所需的角色、权限、范围和成员。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_with_details(self) -> list[Role]:
        """按内置角色优先、ID 稳定排序读取全部角色详情。"""

        result = await self.db.execute(
            select(Role)
            .options(
                selectinload(Role.permissions),
                selectinload(Role.default_departments),
                selectinload(Role.assignments).selectinload(UserRoleAssignment.user),
            )
            .order_by(Role.is_builtin.desc(), Role.id.asc())
        )
        return list(result.scalars().unique().all())
