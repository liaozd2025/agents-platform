"""角色定义与安全审计数据访问。"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from yuxi.storage.postgres.models_business import Role, SecurityAudit, User, UserRoleAssignment


class RoleRepository:
    """读写角色生命周期所需的角色、权限、范围、成员和审计。"""

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

    async def get_with_details(self, role_id: int) -> Role | None:
        """按 ID 读取一个角色及其权限、范围和成员。"""

        result = await self.db.execute(
            select(Role)
            .options(
                selectinload(Role.permissions),
                selectinload(Role.default_departments),
                selectinload(Role.assignments).selectinload(UserRoleAssignment.user),
            )
            .where(Role.id == role_id)
            .execution_options(populate_existing=True)
        )
        return result.scalars().unique().one_or_none()

    async def get_by_code(self, code: str) -> Role | None:
        """按稳定标识读取角色。"""

        return await self.db.scalar(select(Role).where(Role.code == code))

    async def count_active_members(self, role_id: int) -> int:
        """统计仍在使用指定角色的有效用户。"""

        return int(
            await self.db.scalar(
                select(func.count(UserRoleAssignment.id))
                .join(User, User.id == UserRoleAssignment.user_id)
                .where(UserRoleAssignment.role_id == role_id, User.is_deleted == 0)
            )
            or 0
        )

    async def list_role_audits(self, role_ids: list[int]) -> dict[int, list[SecurityAudit]]:
        """按角色分组读取最近的安全审计，组内按发生顺序排列。"""

        grouped = {role_id: [] for role_id in role_ids}
        if not role_ids:
            return grouped

        # ponytail: 总览只取最近 200 条；需要长期检索时再拆独立分页端点。
        audit_limit = 200
        result = await self.db.execute(
            select(SecurityAudit)
            .options(selectinload(SecurityAudit.actor))
            .where(SecurityAudit.target_type == "role", SecurityAudit.target_id.in_(role_ids))
            .order_by(SecurityAudit.id.desc())
            .limit(audit_limit)
        )
        for audit in reversed(list(result.scalars())):
            grouped[audit.target_id].append(audit)
        return grouped
