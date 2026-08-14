from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from yuxi.repositories.user_repository import UserRepository
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Role, UserRoleAssignment


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

    monkeypatch.setattr(pg_manager, "get_async_session_context", session_context)

    user = await UserRepository().create(
        {
            "username": "new-user",
            "uid": "new-user",
            "password_hash": "unused",
            "role": "user",
        }
    )

    assert session.added[0] is user
    assignment = session.added[1]
    assert isinstance(assignment, UserRoleAssignment)
    assert assignment.user is user
    assert assignment.role is role
    assert assignment.scope_mode == "inherit"
    session.commit.assert_awaited_once()
