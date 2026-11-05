"""API Key 功能权限、owner 数据范围及 APP 历史统计的真实 PostgreSQL/HTTP 证据。"""

import asyncio
import os
import socket
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.infrastructure.postgres.base import Base
from yuxi.modules.agents.models.inputs import AgentInput
from yuxi.modules.agents.models.sessions import Session
from yuxi.modules.identity.models import APIKey, Department, Role, RolePermission, User, UserRoleAssignment
from yuxi.modules.identity.repositories.api_keys import APIKeyRepository, APIKeySubjectUnavailable
from yuxi.modules.identity.repositories.users import UserRepository
from yuxi.modules.identity.security import AuthUtils
from yuxi.modules.identity.services import api_keys
from yuxi.modules.workspace.models import Project
from yuxi.shared.datetime import utc_now
from yuxi.shared.errors import PermissionDeniedError

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


@pytest_asyncio.fixture
async def scenario(monkeypatch):
    """独立 Schema 装配同名 APP、不同组织 owner，完全隔离共享业务数据。"""
    schema = "pytest_api_scope_" + uuid4().hex
    admin = create_async_engine(os.environ["POSTGRES_URL"])
    engine = create_async_engine(os.environ["POSTGRES_URL"], connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setenv("API_KEY_DERIVATION_SECRET", "pytest-only-api-key-scope-derivation-secret")
    try:
        async with admin.begin() as db:
            await db.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        async with factory() as db:
            a, b = Department(name="Scope A"), Department(name="Scope B")
            db.add_all([a, b])
            await db.flush()
            a.path, b.path = f"/{a.id}/", f"/{b.id}/"
            users = {
                name: User(username=name, uid=name, password_hash="!fixture", department_id=department.id, role_assignments=[])
                for name, department in [("manager", a), ("inside", a), ("outside", b)]
            }
            db.add_all(users.values())
            role = Role(code="key-manager", name="Key manager", default_scope_type="organization_and_descendants")
            role.permissions = [RolePermission(permission_key="api_key:manage_all")]
            unrelated = Role(code="wide-other", name="Other all-scope permission", default_scope_type="all")
            unrelated.permissions = [RolePermission(permission_key="user:read")]
            db.add_all([role, unrelated])
            await db.flush()
            users["manager"].role_assignments = [UserRoleAssignment(role=role), UserRoleAssignment(role=unrelated)]
            end_user = User(
                username="end-user",
                uid="end-user",
                password_hash="!fixture",
                user_kind="end_user",
                owner_user_id=users["inside"].id,
                app_id="same-app",
                end_user_id="one",
            )
            db.add(end_user)
            await db.flush()
            users["end_user"] = end_user
            ids = {name: user.id for name, user in users.items()}
            await db.commit()
        yield {"db": factory, "ids": ids}
    finally:
        await engine.dispose()
        async with admin.begin() as db:
            await db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


async def actor(db, scenario, name):
    """按正式 repository 载入完整角色与组织祖先上下文。"""
    return await UserRepository().get_by_id_with_db(db, scenario["ids"][name])


async def test_key_management_keeps_self_access_and_scopes_other_owners(scenario):
    """功能与范围必须来自同一角色分配，自己的 Key 不要求管理全员权限。"""
    async with scenario["db"]() as db:
        manager = await actor(db, scenario, "manager")
        own = await api_keys.create_api_key(db, actor=manager, request_id="own", name="own")
        inside = await api_keys.create_api_key(db, actor=manager, request_id="inside", name="inside", user_id=scenario["ids"]["inside"])
        with pytest.raises(PermissionDeniedError):
            await api_keys.create_api_key(
                db, actor=manager, request_id="outside-denied", name="outside", user_id=scenario["ids"]["outside"]
            )
        outside_actor = await actor(db, scenario, "outside")
        outside = await api_keys.create_api_key(db, actor=outside_actor, request_id="outside-own", name="private")
        manager = await actor(db, scenario, "manager")
        listed = await api_keys.list_api_keys(db, actor=manager, skip=0, limit=10)
        assert {row["id"] for row in listed["api_keys"]} == {own["api_key"]["id"], inside["api_key"]["id"]}
        assert listed["total"] == 2
        for operation in [api_keys.get_api_key, api_keys.revoke_api_key]:
            manager = await actor(db, scenario, "manager")
            with pytest.raises(PermissionDeniedError):
                await operation(db, actor=manager, api_key_id=outside["api_key"]["id"])
        manager = await actor(db, scenario, "manager")
        with pytest.raises(PermissionDeniedError):
            await api_keys.update_api_key(db, actor=manager, api_key_id=outside["api_key"]["id"], updates={"name": "stolen"})
        outside_actor = await actor(db, scenario, "outside")
        changed = await api_keys.update_api_key(db, actor=outside_actor, api_key_id=outside["api_key"]["id"], updates={"name": "own-edit"})
        assert changed["api_key"]["name"] == "own-edit"
        assert await db.scalar(select(func.count(APIKey.id))) == 3
        assert (await db.get(APIKey, outside["api_key"]["id"])).revoked_at is None
        with pytest.raises(PermissionDeniedError):
            await api_keys.list_app_overview(db, actor=outside_actor)


async def test_end_user_cannot_issue_keys_even_through_repository(scenario):
    """APP 终端用户不能自行签发，也不能被管理者指定为凭据 owner。"""
    async with scenario["db"]() as db:
        user = await actor(db, scenario, "end_user")
        with pytest.raises(PermissionDeniedError):
            await api_keys.create_api_key(db, actor=user, request_id="end-user", name="forbidden")
        manager = await actor(db, scenario, "manager")
        with pytest.raises(PermissionDeniedError):
            await api_keys.create_api_key(db, actor=manager, request_id="end-owner", name="forbidden", user_id=scenario["ids"]["end_user"])
        with pytest.raises(APIKeySubjectUnavailable):
            await APIKeyRepository(db).create(
                key_hash="a" * 64,
                key_prefix="yxkey_test",
                request_id="direct",
                name="forbidden",
                user_id=scenario["ids"]["end_user"],
                department_id=None,
                expires_at=None,
                created_by="fixture",
            )
        assert await db.scalar(select(func.count(APIKey.id))) == 0


async def seed_app_facts(scenario):
    """写入同名 APP 的不同 owner、撤销历史凭据及带可信凭据快照的输入。"""
    async with scenario["db"]() as db:
        keys = []
        for label, owner, revoked, app_id in [
            ("own", "inside", False, "same-app"),
            ("revoked", "inside", True, None),
            ("foreign", "outside", False, "same-app"),
        ]:
            key = APIKey(
                key_hash=uuid4().hex + uuid4().hex,
                key_prefix="yxkey_test",
                name=label,
                user_id=scenario["ids"][owner],
                created_by="fixture",
                app_id=app_id,
                revoked_at=utc_now() if revoked else None,
                is_enabled=not revoked,
            )
            db.add(key)
            keys.append(key)
        for i in range(2):
            db.add(
                User(
                    username=f"foreign-{i}",
                    uid=f"foreign-{i}",
                    password_hash="!fixture",
                    user_kind="end_user",
                    owner_user_id=scenario["ids"]["outside"],
                    app_id="same-app",
                    end_user_id=str(i),
                )
            )
        db.add(
            User(
                username="deleted",
                uid="deleted",
                password_hash="!fixture",
                user_kind="end_user",
                is_deleted=1,
                owner_user_id=scenario["ids"]["inside"],
                app_id="same-app",
                end_user_id="deleted",
            )
        )
        project = Project(
            id="project",
            uid="inside",
            name="fixture",
            workdir_path="projects/fixture",
            selection_status="implicit",
            directory_mode="managed",
        )
        db.add(project)
        await db.flush()
        db.add(Session(thread_id="thread", uid="inside", app_id="same-app", agent_id="fixture", project_id=project.id))
        await db.flush()
        for i, (key, source, app_id) in enumerate(
            [
                (keys[0], "public_api", "same-app"),
                (keys[1], "public_api", "historical-app"),
                (keys[2], "public_api", "same-app"),
                (keys[2], "chat", "same-app"),
                (keys[0], "cooperation", "same-app"),
                (None, "public_api", "same-app"),
            ]
        ):
            db.add(
                AgentInput(
                    id=f"input-{i}",
                    thread_id="thread",
                    uid="inside",
                    app_id=app_id,
                    api_key_id=key.id if key else None,
                    agent_slug="fixture",
                    kind="follow_up",
                    status="cancelled",
                    messages=[{"type": "text", "text": "fixture"}],
                    source=source,
                    cancelled_at=utc_now(),
                )
            )
        await db.commit()


async def test_app_overview_is_owner_scoped_and_retains_revoked_history(scenario):
    """同名 APP 不能借其他 owner 增加用户或输入计数，撤销/解绑不抹去历史。"""
    await seed_app_facts(scenario)
    async with scenario["db"]() as db:
        result = await api_keys.list_app_overview(db, actor=await actor(db, scenario, "manager"))
        apps = {app["app_id"]: app for app in result["apps"]}
        assert set(apps) == {"same-app", "historical-app"}
        assert [key["name"] for key in apps["same-app"]["keys"]] == ["own"]
        assert apps["same-app"]["end_user_count"] == apps["same-app"]["input_count"] == 1
        assert apps["historical-app"]["keys"] == []
        assert apps["historical-app"]["input_count"] == 1
        assert result["totals"] == {"app_count": 2, "key_count": 1, "end_user_count": 1, "input_count": 2}


async def test_http_keys_and_app_overview_observe_the_same_scope(scenario):
    """真实 TCP 路由使用相同 owner 范围，并回读持久 Key 验证拒绝无副作用。"""
    from yuxi.api.dependencies.auth import get_db
    from yuxi.api.errors import domain_error_handler
    from yuxi.api.routers.identity.users import user_router
    from yuxi.shared.errors import DomainError

    await seed_app_facts(scenario)
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.include_router(user_router, prefix="/api")

    async def request_db():
        """请求借用独立 Schema，实际服务拥有提交。"""
        async with scenario["db"]() as db:
            yield db

    app.dependency_overrides[get_db] = request_db
    headers = {
        name: {"Authorization": "Bearer " + AuthUtils.create_access_token({"sub": str(user_id), "auth_method": "password"})}
        for name, user_id in scenario["ids"].items()
    }
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="critical", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", trust_env=False) as client:
            response = await client.get("/api/user/apikey/apps", headers=headers["manager"])
            assert response.status_code == 200, response.text
            assert response.json()["totals"]["input_count"] == 2
            assert (await client.get("/api/user/apikey/apps", headers=headers["inside"])).status_code == 403
            payload = {"request_id": "http-owned", "name": "http", "user_id": scenario["ids"]["inside"]}
            created = await client.post("/api/user/apikey/", headers=headers["manager"], json=payload)
            assert created.status_code == 200, created.text
            key_id = created.json()["api_key"]["id"]
            denied = await client.put(f"/api/user/apikey/{key_id}", headers=headers["outside"], json={"name": "stolen"})
            assert denied.status_code == 403, denied.text
            async with scenario["db"]() as db:
                assert (await db.get(APIKey, key_id)).name == "http"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()
