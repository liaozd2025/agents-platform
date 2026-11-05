"""真实 HTTP 与独立 PostgreSQL Schema 验证 APP 用户管理沿 owner 的当前组织授权。"""

import asyncio
import os
import socket
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import uvicorn
from fastapi import Depends, FastAPI, Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from yuxi.api.dependencies.auth import get_db, get_required_user
from yuxi.api.errors import domain_error_handler
from yuxi.infrastructure.postgres.base import BusinessBase
from yuxi.infrastructure.postgres.models import load_models
from yuxi.modules.identity.models import Department, Role, RoleDefaultDepartment, RolePermission, User, UserRoleAssignment
from yuxi.modules.identity.repositories.users import UserRepository
from yuxi.shared.errors import DomainError

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


@pytest_asyncio.fixture
async def identity_database():
    """创建一次性结构和真实多角色身份，退出时只删除本例 Schema。"""
    schema = "managed_app_" + uuid4().hex
    dsn = os.environ["POSTGRES_URL"]
    owner_engine = create_async_engine(dsn)
    engine = create_async_engine(dsn, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    load_models()
    try:
        async with owner_engine.begin() as db:
            await db.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as db:
            await db.run_sync(BusinessBase.metadata.create_all)
        async with factory() as db:
            departments = [
                Department(id=1, name="集团", parent_id=None, path="/1/"),
                Department(id=2, name="甲组织", parent_id=1, path="/1/2/"),
                Department(id=3, name="乙组织", parent_id=1, path="/1/3/"),
                Department(id=4, name="范围外", parent_id=1, path="/1/4/"),
                Department(id=5, name="甲下级", parent_id=2, path="/1/2/5/"),
            ]
            db.add_all(departments)
            await db.flush()
            manage_a = Role(
                code="manage_a",
                name="甲管理",
                default_scope_type="selected_organizations_and_descendants",
                permissions=[RolePermission(permission_key=key) for key in ("user:read", "user:delete", "user:update", "user:role_assign")],
                default_departments=[RoleDefaultDepartment(department_id=2)],
            )
            read_b = Role(
                code="read_b",
                name="乙只读",
                default_scope_type="selected_organizations_and_descendants",
                permissions=[RolePermission(permission_key="user:read")],
                default_departments=[RoleDefaultDepartment(department_id=3)],
            )
            employee = Role(code="employee_custom", name="员工", default_scope_type="self", permissions=[], default_departments=[])
            reviewer = Role(code="reviewer_custom", name="审核员", default_scope_type="self", permissions=[], default_departments=[])
            db.add_all([manage_a, read_b, employee, reviewer])
            await db.flush()
            users = [
                User(
                    id=10,
                    uid="manager",
                    username="管理员",
                    password_hash="!disabled",
                    department_id=4,
                    role_assignments=[
                        UserRoleAssignment(role=role, scope_mode="inherit", scope_departments=[]) for role in (manage_a, read_b)
                    ],
                ),
                User(
                    id=11,
                    uid="owner_a",
                    username="甲用户",
                    password_hash="!disabled",
                    department_id=5,
                    role_assignments=[
                        UserRoleAssignment(role=role, scope_mode="inherit", scope_departments=[]) for role in (employee, reviewer)
                    ],
                ),
                User(
                    id=12,
                    uid="owner_b",
                    username="乙用户",
                    password_hash="!disabled",
                    department_id=3,
                    role_assignments=[UserRoleAssignment(role=reviewer, scope_mode="inherit", scope_departments=[])],
                ),
                User(id=13, uid="owner_outside", username="范围外用户", password_hash="!disabled", department_id=4, role_assignments=[]),
                User(
                    id=14,
                    uid="owner_deleted",
                    username="已删除所有者",
                    password_hash="!disabled",
                    department_id=2,
                    is_deleted=1,
                    role_assignments=[],
                ),
            ]
            db.add_all(users)
            await db.flush()
            for identity, owner_id, department_id in ((20, 11, None), (21, 12, None), (22, 13, 2), (23, 14, 2), (24, 11, None)):
                db.add(
                    User(
                        id=identity,
                        uid=f"app_{identity}",
                        username=f"app_{identity}",
                        password_hash="!disabled",
                        user_kind="end_user",
                        owner_user_id=owner_id,
                        app_id=f"app-{identity}",
                        end_user_id="visitor",
                        department_id=department_id,
                        role_assignments=[],
                    )
                )
            await db.commit()
        yield factory
    finally:
        await engine.dispose()
        async with owner_engine.begin() as db:
            await db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await owner_engine.dispose()


@pytest_asyncio.fixture
async def scenario(identity_database):
    """通过真实 TCP HTTP 装配用户路由，仅替换凭据识别。"""
    from yuxi.api.routers.identity.auth import auth

    factory = identity_database
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.include_router(auth, prefix="/api")

    async def request_db():
        """独立请求会话不自动提交，服务拥有写入事务。"""
        async with factory() as db:
            yield db

    async def request_user(request: Request, db: AsyncSession = Depends(get_db)):
        """用固定测试身份替换登录凭据，完整角色从当前数据库读取。"""
        return await UserRepository(db).get_by_id(int(request.headers.get("X-Test-Actor", "10")))

    app.dependency_overrides[get_db] = request_db
    app.dependency_overrides[get_required_user] = request_user
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="critical", access_log=False))
    task = None
    try:
        task = asyncio.create_task(server.serve(sockets=[sock]))
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", trust_env=False) as client:
            yield {"client": client, "db": factory}
    finally:
        if task is not None:
            server.should_exit = True
            await asyncio.wait_for(task, 10)
        sock.close()


async def test_postgres_self_scope_only_matches_current_human_owner(identity_database):
    """self 范围覆盖自己的 APP，不放行同组织其他 owner。"""
    from yuxi.modules.identity.permissions import build_authorization_context
    from yuxi.modules.identity.services.management import read_users_page

    async with identity_database() as db:
        role = await db.scalar(select(Role).where(Role.code == "manage_a"))
        role.default_scope_type = "self"
        read_b = await db.scalar(select(Role).where(Role.code == "read_b"))
        read_b.is_active = False
        db.add(
            User(
                id=25,
                uid="own_app",
                username="own_app",
                password_hash="!disabled",
                user_kind="end_user",
                owner_user_id=10,
                app_id="own-app",
                end_user_id="visitor",
                role_assignments=[],
            )
        )
        await db.commit()
    async with identity_database() as db:
        actor = await UserRepository(db).get_by_id(10)
        page = await read_users_page(user_kind="all", authorization=build_authorization_context(actor), db=db)
        assert page["total"] == 2
        assert [item["id"] for item in page["items"]] == [10, 25]
        assert page["items"][1]["roles"] == [] and page["items"][1]["can_delete"]


async def test_page_scope_counts_multiple_roles_and_app_identity(scenario):
    """默认真人、APP 和全部分页均受 owner 范围限制，多个角色不放大计数。"""
    client = scenario["client"]
    response = await client.get("/api/auth/users/page")
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [11, 12]
    assert response.json()["total"] == 2
    pages = []
    for offset in range(3):
        response = await client.get("/api/auth/users/page", params={"user_kind": "end_user", "limit": 1, "offset": offset})
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 3
        pages.extend(response.json()["items"])
    assert [item["id"] for item in pages] == [20, 21, 24]
    assert all(item["user_kind"] == "end_user" and item["roles"] == [] for item in pages)
    assert [item["app_id"] for item in pages] == ["app-20", "app-21", "app-24"]
    assert [item["can_delete"] for item in pages] == [True, False, True]
    result = await client.get("/api/auth/users", params={"user_kind": "all"})
    assert result.status_code == 200, result.text
    assert result.headers["X-Total-Count"] == "5"
    assert [item["id"] for item in result.json()] == [11, 12, 20, 21, 24]
    filtered = await client.get(
        "/api/auth/users/page",
        params={
            "user_kind": "all",
            "department_ids": "2,3",
            "roles": "employee_custom,reviewer_custom",
            "limit": 1,
        },
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 2
    assert [item["id"] for item in filtered.json()["items"]] == [11]
    direct = await client.get("/api/auth/users", params={"user_kind": "end_user", "department_ids": "2", "direct": "true"})
    assert direct.status_code == 200 and direct.json() == []
    for params in (
        {"user_kind": "invalid"},
        {"department_ids": "2,bad"},
        {"department_ids": "0"},
        {"roles": "employee_custom,,reviewer_custom"},
    ):
        assert (await client.get("/api/auth/users/page", params=params)).status_code == 422
    assert (await client.get("/api/auth/users/page", params={"department_ids": "2,4"})).status_code == 404


async def test_app_detail_delete_and_role_rejection_persist(scenario):
    """读取和删除使用不同授权范围；越权无写入，成功删除可由独立会话回读。"""
    client = scenario["client"]
    for identity in (20, 21):
        response = await client.get(f"/api/auth/users/{identity}")
        assert response.status_code == 200, response.text
        assert response.json()["app_id"] == f"app-{identity}"
    for identity in (22, 23):
        assert (await client.get(f"/api/auth/users/{identity}")).status_code == 404
        assert (await client.delete(f"/api/auth/users/{identity}")).status_code == 404
    assert (await client.delete("/api/auth/users/21")).status_code == 404
    changed = await client.put("/api/auth/users/20", json={"role_assignments": [{"role_id": 1, "scope_mode": "inherit"}]})
    assert changed.status_code in (403, 404), changed.text
    async with scenario["db"]() as db:
        assert await db.scalar(select(func.count()).select_from(UserRoleAssignment).where(UserRoleAssignment.user_id >= 20)) == 0
        assert (await db.get(User, 21)).is_deleted == 0
        assert (await db.get(User, 22)).is_deleted == 0
    deleted = await client.delete("/api/auth/users/20")
    assert deleted.status_code == 200, deleted.text
    async with scenario["db"]() as db:
        user = await db.get(User, 20)
        assert user.is_deleted == 1 and user.deleted_at is not None
        assert (await db.get(User, 11)).is_deleted == 0
    assert (await client.get("/api/auth/users/20")).status_code == 404


async def test_owner_move_and_deletion_change_app_scope_without_copying_roles(scenario):
    """owner 调离授权组织后 APP 的旧组织字段不能继续放行。"""
    client = scenario["client"]
    async with scenario["db"]() as db:
        owner = await db.get(User, 11)
        owner.department_id = 4
        app_user = await db.get(User, 24)
        app_user.department_id = 2
        await db.commit()
    response = await client.get("/api/auth/users/page", params={"user_kind": "end_user"})
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [21]
    assert (await client.get("/api/auth/users/24")).status_code == 404
    assert (await client.delete("/api/auth/users/24")).status_code == 404
    async with scenario["db"]() as db:
        assert (await db.get(User, 24)).is_deleted == 0
        owner = await db.get(User, 12)
        owner.is_deleted = 1
        await db.commit()
    response = await client.get("/api/auth/users/page", params={"user_kind": "end_user"})
    assert response.status_code == 200 and response.json()["total"] == 0
    assert (await client.get("/api/auth/users/21")).status_code == 404
