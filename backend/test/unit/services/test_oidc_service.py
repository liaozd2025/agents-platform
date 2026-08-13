from __future__ import annotations

import os
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("OPENAI_API_KEY", "dummy")

from yuxi.services import oidc_service
from yuxi.storage.postgres.models_business import GROUP_NODE_TYPE, ROOT_DEPARTMENT_ID, Department, User


pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture
async def oidc_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Department.__table__.create)
        await conn.run_sync(User.__table__.create)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            Department(
                id=ROOT_DEPARTMENT_ID,
                name="集团",
                parent_id=None,
                node_type=GROUP_NODE_TYPE,
                path=f"/{ROOT_DEPARTMENT_ID}/",
            )
        )
        await session.commit()
        yield session

    await engine.dispose()


async def _create_user(session, uid: str = "alice") -> User:
    user = User(username="alice", uid=uid, password_hash="x", role="user", is_deleted=0)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def test_resolve_oidc_department_returns_unique_exact_match(oidc_session):
    department = Department(name="研发部", parent_id=ROOT_DEPARTMENT_ID, path=f"/{ROOT_DEPARTMENT_ID}/2/")
    oidc_session.add(department)
    await oidc_session.commit()

    resolved = await oidc_service.resolve_oidc_department(oidc_session, "研发部")

    assert resolved.id == department.id


async def test_resolve_oidc_department_falls_back_to_root_when_no_exact_match(oidc_session, monkeypatch):
    oidc_session.add(Department(name="研发部", parent_id=ROOT_DEPARTMENT_ID, path=f"/{ROOT_DEPARTMENT_ID}/2/"))
    await oidc_session.commit()
    count_before = await oidc_session.scalar(select(func.count(Department.id)))
    warnings = []
    monkeypatch.setattr(oidc_service, "logger", SimpleNamespace(info=lambda _: None, warning=warnings.append))

    resolved = await oidc_service.resolve_oidc_department(oidc_session, "集团/研发部")

    assert resolved.id == ROOT_DEPARTMENT_ID
    assert await oidc_session.scalar(select(func.count(Department.id))) == count_before
    assert len(warnings) == 1


async def test_resolve_oidc_department_falls_back_to_root_when_name_is_duplicated(oidc_session, monkeypatch):
    company_a = Department(name="A 公司", parent_id=ROOT_DEPARTMENT_ID, path=f"/{ROOT_DEPARTMENT_ID}/2/")
    company_b = Department(name="B 公司", parent_id=ROOT_DEPARTMENT_ID, path=f"/{ROOT_DEPARTMENT_ID}/3/")
    oidc_session.add_all([company_a, company_b])
    await oidc_session.flush()
    oidc_session.add_all(
        [
            Department(name="财务部", parent_id=company_a.id, path=f"{company_a.path}4/"),
            Department(name="财务部", parent_id=company_b.id, path=f"{company_b.path}5/"),
        ]
    )
    await oidc_session.commit()
    warnings = []
    monkeypatch.setattr(oidc_service, "logger", SimpleNamespace(info=lambda _: None, warning=warnings.append))

    resolved = await oidc_service.resolve_oidc_department(oidc_session, "财务部")

    assert resolved.id == ROOT_DEPARTMENT_ID
    assert len(warnings) == 1


async def test_find_user_by_oidc_sub_resolves_placeholder_when_sub_contains_colon(oidc_session):
    user = await _create_user(oidc_session)

    await oidc_service._create_oidc_binding_placeholder(oidc_session, "tenant:user", user)

    resolved = await oidc_service.find_user_by_oidc_sub(oidc_session, "tenant:user")

    assert resolved is not None
    assert resolved.id == user.id
    assert resolved.uid == user.uid
    assert resolved.is_deleted == 0


async def test_find_deleted_oidc_user_by_sub_resolves_deleted_target_when_sub_contains_colon(oidc_session):
    user = await _create_user(oidc_session)
    user.is_deleted = 1
    await oidc_session.commit()

    await oidc_service._create_oidc_binding_placeholder(oidc_session, "tenant:user", user)

    resolved = await oidc_service.find_deleted_oidc_user_by_sub(oidc_session, "tenant:user")

    assert resolved is not None
    assert resolved.id == user.id
    assert resolved.uid == user.uid
    assert resolved.is_deleted == 1


async def test_oidc_callback_allows_existing_binding_when_sub_contains_colon(oidc_session, monkeypatch):
    user = await _create_user(oidc_session)
    await oidc_service._create_oidc_binding_placeholder(oidc_session, "tenant:user", user)

    monkeypatch.setattr(oidc_service.oidc_config, "enabled", True)
    monkeypatch.setattr(oidc_service.oidc_config, "client_id", "cid")
    monkeypatch.setattr(oidc_service.oidc_config, "client_secret", "secret")
    monkeypatch.setattr(oidc_service.oidc_config, "token_endpoint", "https://example/token")
    monkeypatch.setattr(oidc_service.oidc_config, "authorization_endpoint", "https://example/auth")
    monkeypatch.setattr(oidc_service.oidc_config, "userinfo_endpoint", "https://example/userinfo")
    monkeypatch.setattr(oidc_service.oidc_config, "use_raw_username", True)
    monkeypatch.setattr(oidc_service.oidc_config, "auto_create_user", False)

    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "verify_state",
        classmethod(lambda cls, state: {"redirect_path": "/"}),
    )

    async def fake_exchange(cls, code):
        return {"access_token": "token"}

    async def fake_userinfo(cls, access_token):
        return {"sub": "tenant:user", "preferred_username": "alice"}

    async def fake_log_operation(db, user_id, operation, request=None):
        return None

    monkeypatch.setattr(oidc_service.OIDCUtils, "exchange_code_for_token", classmethod(fake_exchange))
    monkeypatch.setattr(oidc_service.OIDCUtils, "get_userinfo", classmethod(fake_userinfo))
    monkeypatch.setattr(oidc_service, "log_operation", fake_log_operation)

    response = await oidc_service.oidc_callback_handler("dummy-code", "dummy-state", oidc_session)

    assert response.status_code == 302
    assert unquote(response.headers["location"]).startswith("/auth/oidc/callback?code=")
