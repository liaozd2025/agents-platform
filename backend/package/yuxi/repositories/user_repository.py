"""用户数据访问层 - Repository"""

from datetime import UTC
from datetime import datetime as dt
from typing import Annotated, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from yuxi.permissions.authorization import parse_department_ancestor_ids
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import APIKey, Department, Role, User, UserRoleAssignment


def _utc_now() -> dt:
    # 使用 naive datetime 以匹配 PostgreSQL TIMESTAMP WITHOUT TIME ZONE 列
    return dt.now(UTC).replace(tzinfo=None)


def _attach_department_ancestors(user: User, department_path: str | None) -> User:
    """校验并挂载用户所属组织的祖先链。"""

    if user.department_id is not None and not department_path:
        raise ValueError(f"用户 {user.uid} 所属组织节点缺少有效物化路径")
    ancestor_ids = parse_department_ancestor_ids(department_path)
    if user.department_id is not None and (not ancestor_ids or ancestor_ids[-1] != user.department_id):
        raise ValueError(f"用户 {user.uid} 所属组织节点的物化路径无效")
    user.department_ancestor_ids = ancestor_ids
    return user


async def _get_user_with_department_ancestors(db: AsyncSession, criterion: Any) -> User | None:
    """查询用户并一次带出其组织节点祖先链。"""
    result = await db.execute(
        select(User, Department.path)
        .options(
            selectinload(User.role_assignments).selectinload(UserRoleAssignment.scope_departments),
            selectinload(User.role_assignments)
            .selectinload(UserRoleAssignment.role)
            .selectinload(Role.default_departments),
            selectinload(User.role_assignments).selectinload(UserRoleAssignment.role).selectinload(Role.permissions),
        )
        .outerjoin(Department, User.department_id == Department.id)
        .where(criterion)
    )
    row = result.one_or_none()
    if row is None:
        return None

    return _attach_department_ancestors(*row)


class UserRepository:
    """用户数据访问层"""

    async def get_by_id(self, id: int) -> User | None:
        """根据 ID 获取用户"""
        async with pg_manager.get_async_session_context() as session:
            return await self.get_by_id_with_db(session, id)

    async def get_by_id_with_db(self, db: AsyncSession, id: int) -> User | None:
        """使用指定的 db 根据 ID 获取用户"""
        return await _get_user_with_department_ancestors(db, User.id == id)

    async def get_by_uid(self, uid: str) -> User | None:
        """根据 uid 获取用户"""
        async with pg_manager.get_async_session_context() as session:
            return await self.get_by_uid_with_db(session, uid)

    async def get_by_uid_with_db(self, db: AsyncSession, uid: str) -> User | None:
        """使用指定的 db 获取用户"""
        return await _get_user_with_department_ancestors(db, User.uid == uid)

    async def list_by_uids(self, uids: list[str]) -> list[User]:
        """批量获取指定 uid 的用户。"""
        normalized_uids = sorted({str(uid).strip() for uid in uids if str(uid).strip()})
        if not normalized_uids:
            return []

        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(User).where(User.uid.in_(normalized_uids)))
            return list(result.scalars().all())

    async def get_by_phone(self, phone: str) -> User | None:
        """根据手机号获取用户"""
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(User).where(User.phone_number == phone))
            return result.scalar_one_or_none()

    async def list_users(
        self, skip: int = 0, limit: int = 100, department_id: int | None = None, role: str | None = None
    ) -> list[User]:
        """获取用户列表"""
        async with pg_manager.get_async_session_context() as session:
            query = select(User).where(User.is_deleted == 0)
            if department_id is not None:
                query = query.where(User.department_id == department_id)
            if role is not None:
                query = query.where(User.role == role)
            query = query.order_by(User.id.asc()).offset(skip).limit(limit)
            result = await session.execute(query)
            return list(result.scalars().all())

    async def list_with_department(
        self,
        skip: int = 0,
        limit: int | None = 100,
        department_id: int | None = None,
        role: str | None = None,
    ) -> Annotated[list[tuple[User, str | None]], "用户列表，包含组织名称并挂载祖先路径"]:
        """获取用户列表，并带出授权过滤所需的组织名称和路径。"""

        async with pg_manager.get_async_session_context() as session:
            query = (
                select(
                    User,
                    Department.name.label("department_name"),
                    Department.path.label("department_path"),
                )
                .options(
                    selectinload(User.role_assignments).selectinload(UserRoleAssignment.scope_departments),
                    selectinload(User.role_assignments)
                    .selectinload(UserRoleAssignment.role)
                    .selectinload(Role.default_departments),
                )
                .outerjoin(Department, User.department_id == Department.id)
                .where(User.is_deleted == 0)
            )
            if department_id is not None:
                query = query.where(User.department_id == department_id)
            if role is not None:
                query = query.where(User.role == role)
            query = query.order_by(User.id.asc()).offset(skip)
            if limit is not None:
                query = query.limit(limit)
            result = await session.execute(query)
            return [(_attach_department_ancestors(user, path), name) for user, name, path in result.all()]

    async def create(self, data: dict[str, Any]) -> User:
        """创建用户"""
        async with pg_manager.get_async_session_context() as session:
            user = await self.create_with_db(session, data)
            await session.commit()
            await session.refresh(user)
        return user

    async def create_with_db(self, db: AsyncSession, data: dict[str, Any]) -> User:
        """在调用方事务中创建用户并立即绑定迁移期默认角色。"""

        user = User(**data)
        db.add(user)
        await db.flush()

        role = await db.scalar(
            select(Role).options(selectinload(Role.default_departments)).where(Role.code == user.role)
        )
        if role is None:
            raise ValueError(f"用户角色 {user.role} 不存在")
        db.add(UserRoleAssignment(user=user, role=role, scope_mode="inherit"))
        await db.flush()
        return user

    async def update(self, id: int, data: dict[str, Any]) -> User | None:
        """更新用户"""
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(User).where(User.id == id, User.is_deleted == 0))
            user = result.scalar_one_or_none()
            if user is None:
                return None
            for key, value in data.items():
                if key != "id":
                    setattr(user, key, value)
        return user

    async def soft_delete(self, id: int, username: str | None = None, phone_number: str | None = None) -> bool:
        """软删除用户"""
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(User).where(User.id == id, User.is_deleted == 0))
            user = result.scalar_one_or_none()
            if user is None:
                return False
            user.is_deleted = 1

            user.deleted_at = _utc_now()
            if username:
                import hashlib

                hash_suffix = hashlib.sha256(user.uid.encode()).hexdigest()[:4]
                user.username = f"已注销用户-{hash_suffix}"
            if phone_number:
                user.phone_number = None
            api_key_result = await session.execute(select(APIKey).where(APIKey.user_id == user.id))
            for api_key in api_key_result.scalars().all():
                api_key.is_enabled = False
        return True

    async def exists_by_uid(self, uid: str) -> bool:
        """检查 uid 是否存在"""
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(User.id).where(User.uid == uid))
            return result.scalar_one_or_none() is not None

    async def exists_by_phone(self, phone: str) -> bool:
        """检查手机号是否存在"""
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(User.id).where(User.phone_number == phone))
            return result.scalar_one_or_none() is not None

    async def count(self, department_id: int | None = None) -> int:
        """统计用户数量"""
        async with pg_manager.get_async_session_context() as session:
            query = select(func.count(User.id)).where(User.is_deleted == 0)
            if department_id is not None:
                query = query.where(User.department_id == department_id)
            result = await session.execute(query)
            return result.scalar() or 0

    async def get_all_uids(self) -> list[str]:
        """获取所有 uid"""
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(User.uid))
            return [uid for (uid,) in result.all()]

    async def get_admin_count_in_department(self, department_id: int, exclude_user_id: int | None = None) -> int:
        """统计部门中管理员数量"""
        async with pg_manager.get_async_session_context() as session:
            query = select(func.count(User.id)).where(
                User.department_id == department_id, User.role == "admin", User.is_deleted == 0
            )
            if exclude_user_id is not None:
                query = query.where(User.id != exclude_user_id)
            result = await session.execute(query)
            return result.scalar() or 0
