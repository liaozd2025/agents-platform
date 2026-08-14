from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, unquote, urlparse

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("OPENAI_API_KEY", "dummy")

from yuxi.services import oidc_service
from yuxi.storage.postgres.models_business import (
    Base,
    GROUP_NODE_TYPE,
    ROOT_DEPARTMENT_ID,
    Department,
    Role,
    User,
    UserRoleAssignment,
)


pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture
async def oidc_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

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
        session.add(
            Role(
                code="user",
                name="普通用户",
                is_builtin=True,
                is_active=True,
                default_scope_type="self",
            )
        )
        await session.commit()
        yield session

    await engine.dispose()


async def _create_user(session, uid: str = "alice") -> User:
    role = await session.scalar(select(Role).where(Role.code == "user"))
    user = User(username="alice", uid=uid, password_hash="x", is_deleted=0)
    session.add(user)
    session.add(UserRoleAssignment(user=user, role=role, scope_mode="inherit"))
    await session.commit()
    await session.refresh(user)
    return user


async def test_create_oidc_user_always_uses_builtin_user_role(monkeypatch):
    """OIDC 首次登录不能通过外部配置自动获得管理角色。"""

    captured = {}

    class _UserRepository:
        """记录 OIDC 新用户创建参数的最小仓库替身。"""

        async def create(self, data):
            """保存创建参数并返回测试用户。"""

            captured.update(data)
            return User(id=9, **data)

    monkeypatch.setattr(oidc_service, "UserRepository", _UserRepository)
    monkeypatch.setattr(oidc_service.oidc_config, "use_raw_username", False)
    monkeypatch.setattr(oidc_service, "build_unique_oidc_username", AsyncMock(return_value="新用户"))

    await oidc_service.create_oidc_user(
        AsyncMock(),
        {"sub": "new-sub", "name": "新用户", "username": "new-user"},
        ROOT_DEPARTMENT_ID,
    )

    assert "role" not in captured


async def test_resolve_oidc_department_returns_unique_exact_match(oidc_session):
    """唯一同名 claim 应精确挂载已有组织节点。"""
    department = Department(name="研发部", parent_id=ROOT_DEPARTMENT_ID, path=f"/{ROOT_DEPARTMENT_ID}/2/")
    oidc_session.add(department)
    await oidc_session.commit()

    resolved = await oidc_service.resolve_oidc_department(oidc_session, "研发部")

    assert resolved.id == department.id


async def test_resolve_oidc_department_falls_back_to_root_when_no_exact_match(oidc_session, monkeypatch):
    """零命中时应回落集团根且不能解析路径或创建节点。"""
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
    """多个同名节点时应回落集团根而不是猜测。"""
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
    """已有 OIDC 用户登录时也应按本次 claim 更新归属节点。"""
    user = await _create_user(oidc_session)
    await oidc_service._create_oidc_binding_placeholder(oidc_session, "tenant:user", user)
    department = Department(name="研发部", parent_id=ROOT_DEPARTMENT_ID, path=f"/{ROOT_DEPARTMENT_ID}/2/")
    oidc_session.add(department)
    await oidc_session.commit()

    monkeypatch.setattr(oidc_service.oidc_config, "enabled", True)
    monkeypatch.setattr(oidc_service.oidc_config, "client_id", "cid")
    monkeypatch.setattr(oidc_service.oidc_config, "client_secret", "secret")
    monkeypatch.setattr(oidc_service.oidc_config, "token_endpoint", "https://example/token")
    monkeypatch.setattr(oidc_service.oidc_config, "authorization_endpoint", "https://example/auth")
    monkeypatch.setattr(oidc_service.oidc_config, "userinfo_endpoint", "https://example/userinfo")
    monkeypatch.setattr(oidc_service.oidc_config, "use_raw_username", True)
    monkeypatch.setattr(oidc_service.oidc_config, "auto_create_user", False)
    monkeypatch.setattr(oidc_service.oidc_config, "fetch_department_info", True)

    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "verify_state",
        classmethod(lambda cls, state: {"redirect_path": "/"}),
    )

    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "exchange_code_for_token",
        AsyncMock(return_value={"access_token": "token"}),
    )
    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "get_userinfo",
        AsyncMock(return_value={"sub": "tenant:user", "preferred_username": "alice", "department": "研发部"}),
    )
    monkeypatch.setattr(oidc_service, "log_operation", AsyncMock())

    response = await oidc_service.oidc_callback_handler("dummy-code", "dummy-state", oidc_session)

    assert response.status_code == 302
    assert unquote(response.headers["location"]).startswith("/auth/oidc/callback?code=")
    exchange_code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    login_payload = await oidc_service.oidc_exchange_code_handler(exchange_code)
    assert "role" not in login_payload
    assert [role["code"] for role in login_payload["roles"]] == ["user"]
    assert login_payload["effective_permissions"] == []
    await oidc_session.refresh(user)
    assert user.department_id == department.id


async def test_oidc_callback_auto_create_uses_unique_existing_department(oidc_session, monkeypatch):
    """OIDC 自动建用户的主流程应挂载已有唯一节点且不建节点。"""
    department = Department(name="研发部", parent_id=ROOT_DEPARTMENT_ID, path=f"/{ROOT_DEPARTMENT_ID}/2/")
    oidc_session.add(department)
    await oidc_session.commit()
    count_before = await oidc_session.scalar(select(func.count(Department.id)))

    monkeypatch.setattr(oidc_service.oidc_config, "enabled", True)
    monkeypatch.setattr(oidc_service.oidc_config, "client_id", "cid")
    monkeypatch.setattr(oidc_service.oidc_config, "client_secret", "secret")
    monkeypatch.setattr(oidc_service.oidc_config, "token_endpoint", "https://example/token")
    monkeypatch.setattr(oidc_service.oidc_config, "authorization_endpoint", "https://example/auth")
    monkeypatch.setattr(oidc_service.oidc_config, "userinfo_endpoint", "https://example/userinfo")
    monkeypatch.setattr(oidc_service.oidc_config, "use_raw_username", False)
    monkeypatch.setattr(oidc_service.oidc_config, "auto_create_user", True)
    monkeypatch.setattr(oidc_service.oidc_config, "fetch_department_info", True)
    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "verify_state",
        classmethod(lambda cls, state: {"redirect_path": "/"}),
    )

    async def fake_create_user(db, user_info, department_id):
        """在当前测试会话中按回调传入的组织节点创建用户。"""
        user = User(
            username=user_info["username"],
            uid=f"oidc:{user_info['sub']}",
            password_hash="x",
            department_id=department_id,
            is_deleted=0,
        )
        db.add(user)
        role = await db.scalar(select(Role).where(Role.code == "user"))
        db.add(UserRoleAssignment(user=user, role=role, scope_mode="inherit"))
        await db.commit()
        await db.refresh(user)
        return user

    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "exchange_code_for_token",
        AsyncMock(return_value={"access_token": "token"}),
    )
    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "get_userinfo",
        AsyncMock(return_value={"sub": "new-user", "preferred_username": "new-user", "department": "研发部"}),
    )
    monkeypatch.setattr(oidc_service, "create_oidc_user", fake_create_user)
    monkeypatch.setattr(oidc_service, "log_operation", AsyncMock())

    response = await oidc_service.oidc_callback_handler("dummy-code", "dummy-state", oidc_session)
    created_user = await oidc_session.scalar(select(User).where(User.uid == "oidc:new-user"))

    assert response.status_code == 302
    assert created_user.department_id == department.id
    assert await oidc_session.scalar(select(func.count(Department.id))) == count_before
