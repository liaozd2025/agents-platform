"""平台管理接口按功能权限开放的集成测试。"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import (
    ROOT_DEPARTMENT_ID,
    Department,
    OperationLog,
    Role,
    SecurityAudit,
    User,
    UserRoleAssignment,
)
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def platform_permission_users(test_client):
    """创建不依赖外部凭据的平台权限测试账号。"""

    pg_manager.initialize()
    await pg_manager.async_engine.dispose()
    await pg_manager.create_tables()
    await pg_manager.ensure_business_schema()

    suffix = uuid.uuid4().hex[:10]
    password = f"Pw!{uuid.uuid4().hex}"
    async with pg_manager.get_async_session_context() as session:
        root = await session.get(Department, ROOT_DEPARTMENT_ID)
        assert root is not None, "平台权限集成测试需要现有集团根节点"
        roles = {
            role.code: role
            for role in (await session.scalars(select(Role).where(Role.code.in_(("superadmin", "user"))))).all()
        }
        users = [
            User(
                username=f"pytest-platform-admin-{suffix}",
                uid=f"pytest_platform_admin_{suffix}",
                password_hash=AuthUtils.hash_password(password),
                role="superadmin",
                department_id=root.id,
            ),
            User(
                username=f"pytest-platform-standard-{suffix}",
                uid=f"pytest_platform_standard_{suffix}",
                password_hash=AuthUtils.hash_password(password),
                role="user",
                department_id=root.id,
            ),
        ]
        session.add_all(users)
        await session.flush()
        session.add_all(
            [
                UserRoleAssignment(user=users[0], role=roles["superadmin"], scope_mode="inherit"),
                UserRoleAssignment(user=users[1], role=roles["user"], scope_mode="inherit"),
            ]
        )
        user_ids = [user.id for user in users]
        user_uids = [user.uid for user in users]

    headers = []
    for uid in user_uids:
        response = await test_client.post("/api/auth/token", data={"username": uid, "password": password})
        assert response.status_code == 200, response.text
        headers.append({"Authorization": f"Bearer {response.json()['access_token']}"})

    try:
        yield {
            "admin_headers": headers[0],
            "standard_headers": headers[1],
            "standard_user_id": user_ids[1],
        }
    finally:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(delete(SecurityAudit).where(SecurityAudit.actor_user_id.in_(user_ids)))
            await session.execute(delete(OperationLog).where(OperationLog.user_id.in_(user_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await pg_manager.async_engine.dispose()


async def test_platform_capabilities_follow_effective_permissions(
    test_client,
    platform_permission_users,
):
    """平台权限从下一请求生效，个人 API Key 能力保持不变。"""

    admin_headers = platform_permission_users["admin_headers"]
    headers = platform_permission_users["standard_headers"]
    standard_user_id = platform_permission_users["standard_user_id"]
    role_id = None
    cross_key_id = None
    own_key_id = None

    overview = await test_client.get("/api/roles/overview", headers=admin_headers)
    assert overview.status_code == 200, overview.text
    user_role_id = next(role["id"] for role in overview.json()["roles"] if role["code"] == "user")

    created_role = await test_client.post(
        "/api/roles",
        json={
            "code": f"pytest_platform_{uuid.uuid4().hex[:10]}",
            "name": "平台权限测试角色",
            "description": "",
            "permission_keys": [],
            "default_scope_type": "all",
            "default_department_ids": [],
        },
        headers=admin_headers,
    )
    assert created_role.status_code == 201, created_role.text
    role_id = created_role.json()["id"]

    try:
        own_key = await test_client.post(
            "/api/user/apikey/",
            json={"name": "pytest-own-platform-key"},
            headers=headers,
        )
        assert own_key.status_code == 200, own_key.text
        own_key_id = own_key.json()["api_key"]["id"]
        own_list = await test_client.get("/api/user/apikey/", headers=headers)
        assert own_list.status_code == 200, own_list.text
        assert own_key_id in {item["id"] for item in own_list.json()["api_keys"]}

        cross_key = await test_client.post(
            "/api/user/apikey/",
            json={"name": "pytest-cross-platform-key"},
            headers=admin_headers,
        )
        assert cross_key.status_code == 200, cross_key.text
        cross_key_id = cross_key.json()["api_key"]["id"]

        denied_requests = [
            ("post", "/api/system/config", {"json": {"key": "unknown", "value": False}}),
            ("get", "/api/system/logs", {}),
            ("get", "/api/tasks", {}),
            ("get", "/api/system/model-providers", {}),
            ("get", "/api/system/tools", {}),
            ("get", "/api/system/mcp-servers/pytest-missing", {}),
            ("get", "/api/graph/list", {}),
            (
                "post",
                "/api/system/config",
                {"json": {"key": "default_ocr_engine", "value": "pytest-invalid"}},
            ),
            ("get", f"/api/user/apikey/{cross_key_id}", {}),
        ]
        for method, path, kwargs in denied_requests:
            response = await test_client.request(method, path, headers=headers, **kwargs)
            assert response.status_code == 403, (path, response.text)

        assigned = await test_client.put(
            f"/api/auth/users/{standard_user_id}",
            json={"role_assignments": [{"role_id": role_id, "scope_mode": "inherit"}]},
            headers=admin_headers,
        )
        assert assigned.status_code == 200, assigned.text

        capabilities = [
            (
                "system_config:manage",
                "post",
                "/api/system/config",
                {"json": {"key": "unknown", "value": False}},
                400,
            ),
            ("system_log:read", "get", "/api/system/logs", {}, 200),
            ("system_task:manage", "get", "/api/tasks", {}, 200),
            ("model_provider:manage", "get", "/api/system/model-providers", {}, 200),
            ("tool:manage", "get", "/api/system/tools", {}, 200),
            ("mcp:manage", "get", "/api/system/mcp-servers/pytest-missing", {}, 404),
            ("graph:manage", "get", "/api/graph/list", {}, 200),
            (
                "ocr:manage",
                "post",
                "/api/system/config",
                {"json": {"key": "default_ocr_engine", "value": "pytest-invalid"}},
                400,
            ),
            ("api_key:manage_all", "get", f"/api/user/apikey/{cross_key_id}", {}, 200),
        ]
        previous_permission = None
        for permission, method, path, kwargs, expected_status in capabilities:
            updated_role = await test_client.put(
                f"/api/roles/{role_id}",
                json={
                    "name": "平台权限测试角色",
                    "description": "",
                    "permission_keys": [permission],
                    "default_scope_type": "all",
                    "default_department_ids": [],
                },
                headers=admin_headers,
            )
            assert updated_role.status_code == 200, updated_role.text

            profile = await test_client.get("/api/auth/me", headers=headers)
            assert profile.status_code == 200, profile.text
            assert permission in profile.json()["effective_permissions"]
            if previous_permission:
                assert previous_permission not in profile.json()["effective_permissions"]

            response = await test_client.request(method, path, headers=headers, **kwargs)
            assert response.status_code == expected_status, (path, response.text)
            previous_permission = permission

        all_keys = await test_client.get("/api/user/apikey/", headers=headers)
        assert all_keys.status_code == 200, all_keys.text
        assert {own_key_id, cross_key_id}.issubset({item["id"] for item in all_keys.json()["api_keys"]})
    finally:
        if role_id is not None:
            await test_client.put(
                f"/api/auth/users/{standard_user_id}",
                json={"role_assignments": [{"role_id": user_role_id, "scope_mode": "inherit"}]},
                headers=admin_headers,
            )
        for key_id in (own_key_id, cross_key_id):
            if key_id is not None:
                await test_client.delete(f"/api/user/apikey/{key_id}", headers=admin_headers)
        if role_id is not None:
            async with pg_manager.get_async_session_context() as session:
                await session.execute(
                    delete(SecurityAudit).where(
                        SecurityAudit.target_type == "role",
                        SecurityAudit.target_id == role_id,
                    )
                )
                await session.execute(delete(Role).where(Role.id == role_id))
