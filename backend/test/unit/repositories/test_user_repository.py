"""用户 repository 的角色绑定与凭据撤销测试。"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from test.support.sqlite import create_utc_sqlite_engine
from yuxi.infrastructure.postgres.base import Base
from yuxi.modules.identity.models import APIKey, Department, Role, User, UserRoleAssignment
from yuxi.modules.identity.repositories.users import UserRepository
from yuxi.modules.identity.security import AuthUtils
from yuxi.shared.datetime import utc_now

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class _FakeSession:
    def __init__(self, role: Role):
        self.role = role
        self.added: list[object] = []
        self.flush = AsyncMock(side_effect=self._assign_user_id)
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    def add(self, item: object) -> None:
        self.added.append(item)

    async def scalar(self, _statement):
        return self.role

    def _assign_user_id(self) -> None:
        self.added[0].id = 7


@pytest.mark.asyncio
async def test_create_user_adds_matching_role_assignment_in_same_transaction():
    """新用户应立即进入对应角色成员列表，不依赖下次启动回填。"""
    role = Role(id=3, code="user", name="普通用户", default_scope_type="self")
    session = _FakeSession(role)

    user = await UserRepository(session).create_with_db(
        session,
        {
            "username": "new-user",
            "uid": "new-user",
            "password_hash": "unused",
        },
    )

    assert session.added[0] is user
    assignment = user.role_assignments[0]
    assert isinstance(assignment, UserRoleAssignment)
    assert assignment.user is user
    assert assignment.role is role
    assert assignment.scope_mode == "inherit"
    session.commit.assert_not_awaited()


@pytest_asyncio.fixture()
async def user_session():
    """创建带活动 Key 与历史 tombstone 的 SQLite 会话。"""

    engine = create_utc_sqlite_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        user = User(
            username="Delete User",
            uid="delete_user",
            password_hash="$argon2id$placeholder",
        )
        session.add(user)
        await session.flush()
        keys = []
        for name in ("active", "already revoked"):
            _secret, key_hash, key_prefix = AuthUtils.generate_api_key()
            keys.append(
                APIKey(
                    key_hash=key_hash,
                    key_prefix=key_prefix,
                    name=name,
                    user_id=user.id,
                    created_by=str(user.id),
                )
            )
        previous_revocation = utc_now() - timedelta(days=1)
        keys[1].is_enabled = False
        keys[1].revoked_at = previous_revocation
        session.add_all(keys)
        await session.commit()
        yield session, user, keys, previous_revocation
    await engine.dispose()


async def test_user_page_filters_before_pagination_and_excludes_deleted_users(user_session) -> None:
    """分页过滤必须作用于全部有效用户，而不是只过滤当前页。"""

    session, user, _keys, _previous_revocation = user_session
    department = Department(name="Paged Department")
    user_role = Role(code="page-user", name="分页普通用户", default_scope_type="self")
    admin_role = Role(code="page-admin", name="分页管理员", default_scope_type="self")
    session.add_all([department, user_role, admin_role])
    await session.flush()
    department.path = f"/{department.id}/"
    department.path = f"/{department.id}/"
    users = [
        User(
            username=f"Page User {index}",
            uid=f"page_user_{index}",
            phone_number=f"1380000000{index}",
            password_hash="$argon2id$placeholder",
            department_id=department.id,
            is_deleted=1 if index == 1 else 0,
            role_assignments=[UserRoleAssignment(role=user_role if index < 3 else admin_role, scope_mode="inherit")],
        )
        for index in range(4)
    ]
    session.add_all(users)
    await session.commit()

    rows, total = await UserRepository(session).list_with_department_page(
        visibility_clauses=(User.id.is_not(None),),
        skip=1,
        limit=1,
        department_id=department.id,
        role="page-user",
        keyword="page_user_",
    )

    assert total == 2
    assert [row[0].uid for row in rows] == ["page_user_2"]
    assert rows[0][1] == department.name
    assert all(row[0].is_deleted == 0 for row in rows)


async def test_admin_delete_preserves_existing_api_key_tombstones(user_session) -> None:
    """管理员删除停用凭据并保留已有撤销时间。"""

    session, user, keys, previous_revocation = user_session

    await UserRepository(session).delete_for_admin(user)

    assert user.is_deleted == 1
    assert keys[0].is_enabled is False
    assert keys[0].revoked_at is not None
    assert keys[1].is_enabled is False
    assert keys[1].revoked_at == previous_revocation


async def test_user_page_defaults_to_humans_and_filters_kind_before_counting(user_session) -> None:
    """按真实类型筛选，长 ID 前缀不影响系统用户的可见性。"""
    session, owner, _keys, _revocation = user_session
    human = User(username="endusr_human", uid="endusr_human", password_hash="!disabled")
    end_user = User(
        username="visitor",
        uid="endusr_visitor",
        password_hash="!disabled",
        user_kind="end_user",
        owner_user_id=owner.id,
        app_id="support-app",
        end_user_id="visitor",
    )
    session.add_all([human, end_user])
    await session.commit()
    repo = UserRepository(session)
    rows, total = await repo.list_page_with_department(offset=0, limit=1, search="endusr_")
    assert total == 1
    assert [row[0].id for row in rows] == [human.id]
    rows, total = await repo.list_page_with_department(offset=0, limit=1, search="endusr_", user_kind="end_user")
    assert total == 1
    assert [row[0].id for row in rows] == [end_user.id]
    rows, total = await repo.list_page_with_department(offset=1, limit=1, search="endusr_", user_kind=None)
    assert total == 2
    assert [row[0].id for row in rows] == [end_user.id]

    assert [row[0].id for row in await repo.list_with_department(skip=1, limit=1)] == [human.id]
    assert [row[0].id for row in await repo.list_with_department(skip=2, limit=1)] == []
    assert await repo.count() == 2
    assert end_user.uid in await repo.get_all_uids()
    assert (await repo.get_by_uid(end_user.uid)).id == end_user.id
    assert (await repo.get_by_username(end_user.username)).id == end_user.id
