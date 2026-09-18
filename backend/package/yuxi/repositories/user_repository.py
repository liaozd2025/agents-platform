"""用户数据访问层 - Repository"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime as dt
from typing import Annotated, Any

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from yuxi.permissions.authorization import parse_department_ancestor_ids
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import (
    APIKey,
    ScheduledAgentJob,
    Department,
    Role,
    User,
    UserConfig,
    UserRoleAssignment,
)
from yuxi.utils.logging_config import logger


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

    @staticmethod
    async def _delete_scheduled_jobs(session: AsyncSession, uid: str) -> None:
        """账号删除时移除任务定义，数据库级联清理调度历史。"""

        await session.execute(delete(ScheduledAgentJob).where(ScheduledAgentJob.uid == str(uid)))

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

    async def get_memory_profile(self, uid: str):
        """只投影 USER.md 需要的资料字段，不刷新调用方的 ORM 权限关系。

        第 1 项返回展示名：优先 ``display_name``（真实姓名），未维护时回退登录账号 ``username``，
        避免 USER.md 里只出现数字账号。第 2 项返回组织链路（如「集团 > 信息中心 > 开发室」）。
        第 4、5 项分别是 OA 反查到的岗位与职级，未反查到为 ``None``。角色与 UID 不进入用户资料，
        因此这里不联表角色分配，避免同一用户按角色数放大结果行。
        """
        async with self._session() as session:
            result = await session.execute(
                select(
                    User.username,
                    User.display_name,
                    Department.name,
                    UserConfig.enable_memory,
                    User.oa_station_name,
                    User.oa_job_level_name,
                    Department.path,
                )
                .select_from(User)
                .outerjoin(Department, User.department_id == Department.id)
                .outerjoin(UserConfig, UserConfig.uid == User.uid)
                .where(User.uid == uid, User.is_deleted == 0)
            )
            rows = result.all()
            if not rows:
                return None
            # 列序：0=username、1=display_name、2=部门名、3=Memory 开关、4=OA 岗位、5=OA 职级、6=部门物化路径
            (
                username,
                display_name,
                department_name,
                enable_memory,
                station_name,
                job_level_name,
                department_path,
            ) = rows[0]
            department_label = await self._department_chain(session, department_name, department_path)
            return (
                display_name or username,
                department_label,
                enable_memory,
                station_name,
                job_level_name,
            )

    @staticmethod
    async def _department_chain(
        session: AsyncSession, department_name: str | None, department_path: str | None
    ) -> str | None:
        """把部门物化路径还原成「集团 > 信息中心 > 开发室」。

        路径缺失、层级不足、格式异常或链路节点在库中缺失时退回部门名，
        避免把不完整的链路写进用户画像，资料同步不因组织数据问题中断。
        """
        if not department_name:
            return None
        try:
            ancestor_ids = parse_department_ancestor_ids(department_path)
        except (TypeError, ValueError):
            logger.warning(f"部门物化路径无法解析，组织链路退回部门名：department={department_name}")
            return department_name
        if len(ancestor_ids) < 2:
            return department_name
        result = await session.execute(select(Department.id, Department.name).where(Department.id.in_(ancestor_ids)))
        names = {row[0]: row[1] for row in result.all()}
        if any(node_id not in names for node_id in ancestor_ids):
            # 链路中任一节点缺失就说明路径与组织表不一致，宁可用直属部门名也不写残缺链路
            logger.warning(f"组织链路节点缺失，退回部门名：department={department_name}")
            return department_name
        return " > ".join(names[node_id] for node_id in ancestor_ids)

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
        """按 uid、账号、手机号顺序查找登录用户。"""
        async with self._session() as session:
            result = await session.execute(select(User).where(User.uid == identifier))
            user = result.scalar_one_or_none()
            if user is not None:
                return user
            # 旧 OA 的 uid 是稳定身份关联键；username 保留 OA 账号以支持独立账号密码登录。
            result = await session.execute(select(User).where(User.username == identifier))
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

    async def list_with_department_page(
        self,
        *,
        visibility_clauses: tuple[Any, ...],
        department_id: int | None = None,
        direct: bool = False,
        keyword: str | None = None,
        role: str | None = None,
        skip: int = 0,
        limit: int = 100,
        session: AsyncSession | None = None,
    ) -> tuple[list[tuple[User, str | None]], int]:
        """按授权 SQL 条件筛选并分页返回用户，避免先加载完整用户目录。"""

        criteria = [User.is_deleted == 0, or_(*visibility_clauses) if visibility_clauses else False]
        if department_id is not None:
            department_clause = User.department_id == department_id
            if not direct:
                department_clause = Department.path.like(f"%/{department_id}/%")
            criteria.append(department_clause)
        if keyword:
            pattern = f"%{keyword.strip()}%"
            criteria.append(
                or_(
                    User.display_name.ilike(pattern),
                    User.username.ilike(pattern),
                    User.uid.ilike(pattern),
                    User.phone_number.ilike(pattern),
                )
            )
        if role:
            criteria.append(
                exists(
                    select(UserRoleAssignment.id)
                    .join(Role, Role.id == UserRoleAssignment.role_id)
                    .where(
                        UserRoleAssignment.user_id == User.id,
                        Role.code == role,
                    )
                )
            )

        async def execute_queries(db: AsyncSession) -> tuple[list[tuple[User, str | None]], int]:
            base = (
                select(User)
                .select_from(User)
                .outerjoin(Department, User.department_id == Department.id)
                .where(*criteria)
            )
            total = int((await db.scalar(select(func.count()).select_from(base.subquery()))) or 0)
            rows = await db.execute(
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
                .where(*criteria)
                .order_by(User.id.asc())
                .offset(max(skip, 0))
                .limit(max(limit, 0))
            )
            return [(_attach_department_ancestors(user, path), name) for user, name, path in rows.all()], total

        if session is not None:
            return await execute_queries(session)
        async with self._session() as managed_session:
            return await execute_queries(managed_session)

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
            # 显式初始化空范围集合，避免后续替换角色时触发异步关系懒加载。
            role_assignments=[UserRoleAssignment(role=role, scope_mode="inherit", scope_departments=[])],
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
            await self._delete_scheduled_jobs(session, user.uid)
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
            await self._delete_scheduled_jobs(session, user.uid)
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
