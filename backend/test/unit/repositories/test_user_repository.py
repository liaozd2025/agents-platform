"""用户 repository 的角色绑定与凭据撤销测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.user_repository import UserRepository
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import APIKey, Base, Role, User, UserRoleAssignment
from yuxi.utils.auth_utils import AuthUtils
from yuxi.utils.datetime_utils import utc_now_naive

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
async def test_create_user_adds_matching_role_assignment_in_same_transaction(monkeypatch):
    """新用户应立即进入对应角色成员列表，不依赖下次启动回填。"""
    role = Role(id=3, code="user", name="普通用户", default_scope_type="self")
    session = _FakeSession(role)

    @asynccontextmanager
    async def session_context():
        yield session
        await session.commit()

    monkeypatch.setattr(pg_manager, "get_async_session_context", session_context)

    user = await UserRepository().create(
        {
            "username": "new-user",
            "uid": "new-user",
            "password_hash": "unused",
        }
    )

    assert session.added[0] is user
    assignment = user.role_assignments[0]
    assert isinstance(assignment, UserRoleAssignment)
    assert assignment.user is user
    assert assignment.role is role
    assert assignment.scope_mode == "inherit"
    session.commit.assert_awaited_once()


@pytest_asyncio.fixture()
async def user_session():
    """创建带活动 Key 与历史 tombstone 的 SQLite 会话。"""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
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
        previous_revocation = utc_now_naive() - timedelta(days=1)
        keys[1].is_enabled = False
        keys[1].revoked_at = previous_revocation
        session.add_all(keys)
        await session.commit()
        yield session, user, keys, previous_revocation
    await engine.dispose()


async def test_soft_delete_tombstones_all_api_keys_without_rewriting_history(user_session) -> None:
    """通用软删除入口也必须阻止旧请求复活凭据。"""

    session, user, keys, previous_revocation = user_session

    deleted = await UserRepository(session).soft_delete(user.id, username=user.username)

    assert deleted is True
    assert user.is_deleted == 1
    assert keys[0].is_enabled is False
    assert keys[0].revoked_at is not None
    assert keys[1].is_enabled is False
    assert keys[1].revoked_at == previous_revocation
