"""用户数据访问层 - Repository"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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

    def __init__(self, db_session: AsyncSession | None = None):
        self.db_session = db_session

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        """复用请求会话，未注入时创建独立事务会话。"""
        if self.db_session is not None:
            yield self.db_session
            return
        async with pg_manager.get_async_session_context() as session:
            yield session

    async def get_by_id(self, id: int) -> User | None:
        """根据 ID 获取用户"""
        async with self._session() as session:
            return await self.get_by_id_with_db(session, id)

    async def is_first_run(self) -> bool:
        """检查系统是否尚未创建用户。"""
        async with self._session() as session:
            result = await session.execute(select(func.count(User.id)))
            return (result.scalar() or 0) == 0

    async def get_active_by_id(self, id: int, *, for_update: bool = False) -> User | None:
        """根据 ID 获取未删除用户。"""
        async with self._session() as session:
            query = select(User).where(User.id == id, User.is_deleted == 0)
            if for_update:
                query = query.with_for_update()
            result = await session.execute(query)
            return result.scalar_one_or_none()

    @staticmethod
    async def _revoke_api_keys(session: AsyncSession, user_id: int, revoked_at: dt) -> None:
        """撤销用户的全部 API Key，并保留已有撤销时间。"""

        api_key_result = await session.execute(select(APIKey).where(APIKey.user_id == user_id))
        for api_key in api_key_result.scalars().all():
            api_key.is_enabled = False
            if api_key.revoked_at is None:
                api_key.revoked_at = revoked_at

    async def get_by_id_with_db(self, db: AsyncSession, id: int) -> User | None:
        """使用指定的 db 根据 ID 获取用户"""
        return await _get_user_with_department_ancestors(db, User.id == id)

    async def get_by_uid(self, uid: str) -> User | None:
        """根据 uid 获取用户"""
        async with self._session() as session:
            return await self.get_by_uid_with_db(session, uid)

    async def get_by_uid_with_db(self, db: AsyncSession, uid: str) -> User | None:
        """使用指定的 db 获取用户"""
        return await _get_user_with_department_ancestors(db, User.uid == uid)

    async def list_by_uids(self, uids: list[str]) -> list[User]:
        """批量获取指定 uid 的用户。"""
        normalized_uids = sorted({str(uid).strip() for uid in uids if str(uid).strip()})
        if not normalized_uids:
            return []

        async with self._session() as session:
            result = await session.execute(select(User).where(User.uid.in_(normalized_uids)))
            return list(result.scalars().all())

    async def get_by_phone(self, phone: str) -> User | None:
        """根据手机号获取用户"""
        async with self._session() as session:
            result = await session.execute(select(User).where(User.phone_number == phone))
            return result.scalar_one_or_none()

    async def get_by_login_identifier(self, identifier: str) -> User | None:
        """按 uid 优先、手机号兜底查找登录用户。"""
        async with self._session() as session:
            result = await session.execute(select(User).where(User.uid == identifier))
            user = result.scalar_one_or_none()
            if user is not None:
                return user
            result = await session.execute(select(User).where(User.phone_number == identifier))
            return result.scalar_one_or_none()

    async def get_by_username(self, username: str, exclude_user_id: int | None = None) -> User | None:
        """按用户名查找用户，可排除指定用户。"""
        async with self._session() as session:
            query = select(User).where(User.username == username)
            if exclude_user_id is not None:
                query = query.where(User.id != exclude_user_id)
            result = await session.execute(query)
            return result.scalar_one_or_none()

    async def get_by_phone_excluding(self, phone: str, exclude_user_id: int) -> User | None:
        """按手机号查找除指定用户外的用户。"""
        async with self._session() as session:
            result = await session.execute(select(User).where(User.phone_number == phone, User.id != exclude_user_id))
            return result.scalar_one_or_none()

    async def list_users(
        self, skip: int = 0, limit: int = 100, department_id: int | None = None, role: str | None = None
    ) -> list[User]:
        """获取用户列表"""
        async with self._session() as session:
            query = select(User).where(User.is_deleted == 0)
            if department_id is not None:
                query = query.where(User.department_id == department_id)
            if role is not None:
                query = query.join(UserRoleAssignment).join(Role).where(Role.code == role)
            query = query.order_by(User.id.asc()).offset(skip).limit(limit)
            result = await session.execute(query)
            return list(result.scalars().all())

    async def list_with_department(
        self,
        skip: int = 0,
        limit: int | None = 100,
        department_id: int | None = None,
        role: str | None = None,
        *,
        session: AsyncSession | None = None,
    ) -> Annotated[list[tuple[User, str | None]], "用户列表，包含组织名称并挂载祖先路径"]:
        """获取用户列表，并带出授权过滤所需的组织名称和路径。"""

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
            query = query.join(UserRoleAssignment).join(Role).where(Role.code == role)
        query = query.order_by(User.id.asc()).offset(skip)
        if limit is not None:
            query = query.limit(limit)

        if session is not None:
            result = await session.execute(query)
            return [(_attach_department_ancestors(user, path), name) for user, name, path in result.all()]

        async with self._session() as managed_session:
            result = await managed_session.execute(query)
            return [(_attach_department_ancestors(user, path), name) for user, name, path in result.all()]

    async def create(self, data: dict[str, Any], *, default_role_code: str = "user") -> User:
        """创建用户"""
        async with self._session() as session:
            user = await self.create_with_db(session, data, default_role_code=default_role_code)
            await session.refresh(user)
        return user

    async def create_with_db(
        self,
        db: AsyncSession,
        data: dict[str, Any],
        *,
        default_role_code: str = "user",
    ) -> User:
        """在调用方事务中创建用户并绑定默认角色。"""

        role = await db.scalar(
            select(Role).options(selectinload(Role.default_departments)).where(Role.code == default_role_code)
        )
        if role is None:
            raise ValueError(f"用户角色 {default_role_code} 不存在")

        user = User(
            **data,
            role_assignments=[UserRoleAssignment(role=role, scope_mode="inherit")],
        )
        db.add(user)
        await db.flush()
        return user

    async def save(self, user: User, *, refresh: bool = False) -> User:
        """flush 用户实体的当前变更，事务提交由用例 owner 负责。"""
        async with self._session() as session:
            await session.flush()
            if refresh:
                await session.refresh(user)
            return user

    async def update(self, id: int, data: dict[str, Any]) -> User | None:
        """更新用户"""
        async with self._session() as session:
            result = await session.execute(select(User).where(User.id == id, User.is_deleted == 0))
            user = result.scalar_one_or_none()
            if user is None:
                return None
            for key, value in data.items():
                if key != "id":
                    setattr(user, key, value)
            await session.flush()
        return user

    async def soft_delete(self, id: int, username: str | None = None, phone_number: str | None = None) -> bool:
        """软删除用户"""
        async with self._session() as session:
            result = await session.execute(select(User).where(User.id == id, User.is_deleted == 0).with_for_update())
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
            await self._revoke_api_keys(session, user.id, user.deleted_at)
            await session.flush()
        return True

    async def delete_for_admin(self, user: User) -> None:
        """软删除用户并在同一事务中不可恢复地撤销其 API Key。"""
        async with self._session() as session:
            user.is_deleted = 1
            user.deleted_at = _utc_now()
            user.username = f"已注销用户-{user.id}"
            user.phone_number = None
            user.password_hash = "DELETED"
            user.avatar = None
            await self._revoke_api_keys(session, user.id, user.deleted_at)
            await session.flush()

    async def exists_by_uid(self, uid: str) -> bool:
        """检查 uid 是否存在"""
        async with self._session() as session:
            result = await session.execute(select(User.id).where(User.uid == uid))
            return result.scalar_one_or_none() is not None

    async def exists_by_phone(self, phone: str) -> bool:
        """检查手机号是否存在"""
        async with self._session() as session:
            result = await session.execute(select(User.id).where(User.phone_number == phone))
            return result.scalar_one_or_none() is not None

    async def count(self, department_id: int | None = None) -> int:
        """统计用户数量"""
        async with self._session() as session:
            query = select(func.count(User.id)).where(User.is_deleted == 0)
            if department_id is not None:
                query = query.where(User.department_id == department_id)
            result = await session.execute(query)
            return result.scalar() or 0

    async def get_all_uids(self) -> list[str]:
        """获取所有 uid"""
        async with self._session() as session:
            result = await session.execute(select(User.uid))
            return [uid for (uid,) in result.all()]

    async def get_admin_count_in_department(self, department_id: int, exclude_user_id: int | None = None) -> int:
        """统计部门中管理员数量"""
        async with self._session() as session:
            query = (
                select(func.count(func.distinct(User.id)))
                .join(UserRoleAssignment)
                .join(Role)
                .where(User.department_id == department_id, Role.code == "admin", User.is_deleted == 0)
            )
            if exclude_user_id is not None:
                query = query.where(User.id != exclude_user_id)
            result = await session.execute(query)
            return result.scalar() or 0
