from __future__ import annotations

import importlib

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from test.support.sqlite import create_utc_sqlite_engine
from yuxi.api.routers.identity.users import get_logged_in_user, get_user_config, update_user_config
from yuxi.infrastructure.postgres.base import Base
from yuxi.modules.identity.models import Department, User
from yuxi.modules.identity.preferences import UserConfigSchema

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture()
async def session(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("yuxi.modules.identity.services.preferences.sync_user_profile_to_memory", AsyncMock())
    engine = create_utc_sqlite_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        department = Department(name="User Config Dept")
        user_a = User(
            username="User A",
            uid="user_a",
            password_hash="$argon2id$placeholder",
            department=department,
        )
        user_b = User(
            username="User B",
            uid="user_b",
            password_hash="$argon2id$placeholder",
            department=department,
        )
        db.add_all([department, user_a, user_b])
        await db.commit()
        for user in [user_a, user_b]:
            await db.refresh(user)
        yield db, user_a, user_b
    await engine.dispose()


async def test_user_config_routes_scope_to_current_user(session):
    db, user_a, user_b = session

    own_config = await update_user_config(
        UserConfigSchema(enable_memory=True),
        current_user=user_a,
        db=db,
    )
    other_config = await get_user_config(current_user=user_b, db=db)

    assert own_config["enable_memory"] is True
    assert other_config["enable_memory"] is False


async def test_enabling_memory_syncs_user_profile_immediately(session, monkeypatch):
    db, user_a, _ = session
    calls = []

    async def fake_sync(*, db, uid):
        calls.append((db, uid))

    monkeypatch.setattr(importlib.import_module("yuxi.modules.identity.services.preferences"), "sync_user_profile_to_memory", fake_sync)

    await update_user_config(
        UserConfigSchema(enable_memory=True),
        current_user=user_a,
        db=db,
    )

    assert calls == [(db, user_a.uid)]


async def test_user_config_allows_logged_in_user_without_department():
    user = User(
        username="No Dept User",
        uid="no_dept_user",
        password_hash="$argon2id$placeholder",
    )

    assert await get_logged_in_user(user) is user


async def test_memory_sync_failure_is_observable_after_committed_configuration(session, monkeypatch):
    """文件同步故障明确失败，已提交的偏好仍可从数据库读回。"""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "yuxi.modules.identity.services.preferences.sync_user_profile_to_memory", AsyncMock(side_effect=OSError("disk full"))
    )
    db, user, _ = session
    with pytest.raises(OSError, match="disk full"):
        await update_user_config(UserConfigSchema(enable_memory=False), current_user=user, db=db)
    assert (await get_user_config(current_user=user, db=db))["enable_memory"] is False
