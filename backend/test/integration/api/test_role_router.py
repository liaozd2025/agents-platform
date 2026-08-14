"""角色与权限只读总览接口集成测试。"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import OperationLog, Role, SecurityAudit, User, UserRoleAssignment
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_superadmin_can_view_builtin_roles_permissions_scopes_and_members(test_client, admin_headers):
    profile_response = await test_client.get("/api/auth/me", headers=admin_headers)
    assert profile_response.status_code == 200, profile_response.text
    if profile_response.json()["role"] != "superadmin":
        pytest.fail("This test requires TEST_USERNAME to be a superadmin account.")

    response = await test_client.get("/api/roles/overview", headers=admin_headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    permission_keys = {item["key"] for item in payload["permissions"]}
    scope_keys = {item["key"] for item in payload["data_scope_types"]}
    roles = {item["code"]: item for item in payload["roles"]}

    assert set(roles) >= {"superadmin", "admin", "user"}
    assert scope_keys == {
        "none",
        "self",
        "organization_and_descendants",
        "selected_organizations_and_descendants",
        "all",
    }
    assert set(roles["superadmin"]["permission_keys"]) == permission_keys
    assert roles["superadmin"]["default_scope_type"] == "all"
    assert roles["admin"]["default_scope_type"] == "organization_and_descendants"
    assert roles["user"]["default_scope_type"] == "self"
    assert all(roles[code]["is_builtin"] is True for code in ("superadmin", "admin", "user"))
    assert all(roles[code]["is_active"] is True for code in ("superadmin", "admin", "user"))
    assert roles["superadmin"]["member_count"] >= 1
    assert any(member["id"] == profile_response.json()["id"] for member in roles["superadmin"]["members"])


async def test_new_standard_user_is_listed_but_cannot_view_role_overview(
    test_client,
    standard_user,
    admin_headers,
):
    response = await test_client.get("/api/roles/overview", headers=standard_user["headers"])

    assert response.status_code == 403

    overview_response = await test_client.get("/api/roles/overview", headers=admin_headers)
    assert overview_response.status_code == 200, overview_response.text
    user_role = next(role for role in overview_response.json()["roles"] if role["code"] == "user")
    assert any(member["id"] == standard_user["user"]["id"] for member in user_role["members"])


async def _cleanup_custom_roles(role_codes: list[str]) -> None:
    """清理当前用例创建的自定义角色及审计。"""

    async with pg_manager.get_async_session_context() as session:
        role_ids = list((await session.scalars(select(Role.id).where(Role.code.in_(role_codes)))).all())
        if not role_ids:
            return
        await session.execute(
            delete(SecurityAudit).where(
                SecurityAudit.target_type == "role",
                SecurityAudit.target_id.in_(role_ids),
            )
        )
        await session.execute(delete(Role).where(Role.id.in_(role_ids)))


@pytest_asyncio.fixture
async def role_test_users(test_client):
    """创建只服务当前角色 API 用例的两个隔离登录用户。"""

    pg_manager.initialize()
    await pg_manager.async_engine.dispose()
    await pg_manager.create_tables()
    await pg_manager.ensure_business_schema()

    suffix = uuid.uuid4().hex[:10]
    password = f"Pw!{uuid.uuid4().hex}"
    async with pg_manager.get_async_session_context() as session:
        roles = {
            role.code: role
            for role in (await session.scalars(select(Role).where(Role.code.in_(("superadmin", "user"))))).all()
        }
        users = [
            User(
                username=f"pytest-role-admin-{suffix}",
                uid=f"pytest_role_admin_{suffix}",
                password_hash=AuthUtils.hash_password(password),
                role="superadmin",
            ),
            User(
                username=f"pytest-role-user-{suffix}",
                uid=f"pytest_role_user_{suffix}",
                password_hash=AuthUtils.hash_password(password),
                role="user",
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

    headers = []
    for user in users:
        response = await test_client.post(
            "/api/auth/token",
            data={"username": user.uid, "password": password},
        )
        assert response.status_code == 200, response.text
        headers.append({"Authorization": f"Bearer {response.json()['access_token']}"})

    try:
        yield {
            "admin_headers": headers[0],
            "standard": {"user": {"id": user_ids[1]}, "headers": headers[1]},
        }
    finally:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(delete(SecurityAudit).where(SecurityAudit.actor_user_id.in_(user_ids)))
            await session.execute(delete(OperationLog).where(OperationLog.user_id.in_(user_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await pg_manager.async_engine.dispose()


async def test_superadmin_can_manage_custom_role_lifecycle_with_structured_audits(test_client, role_test_users):
    """创建、复制、修改和停用均应保留可查询的变更前后值。"""

    admin_headers = role_test_users["admin_headers"]
    suffix = uuid.uuid4().hex[:10]
    role_code = f"pytest_role_{suffix}"
    copy_code = f"pytest_role_copy_{suffix}"

    try:
        create_response = await test_client.post(
            "/api/roles",
            json={
                "code": role_code,
                "name": "测试安全审计员",
                "description": "测试角色生命周期",
                "permission_keys": ["role:read", "dashboard:view"],
                "default_scope_type": "self",
                "default_department_ids": [],
            },
            headers=admin_headers,
        )
        assert create_response.status_code == 201, create_response.text
        created = create_response.json()
        assert created["is_builtin"] is False
        assert created["is_active"] is True
        assert created["audits"][-1]["action"] == "role.create"
        assert created["audits"][-1]["before"] is None
        assert created["audits"][-1]["after"]["permission_keys"] == ["role:read", "dashboard:view"]
        assert created["audits"][-1]["actor"]["id"] is not None
        assert created["audits"][-1]["target"] == {
            "type": "role",
            "id": created["id"],
            "code": role_code,
        }

        copy_response = await test_client.post(
            f"/api/roles/{created['id']}/copy",
            json={"code": copy_code, "name": "测试安全审计员副本"},
            headers=admin_headers,
        )
        assert copy_response.status_code == 201, copy_response.text
        copied = copy_response.json()
        assert copied["permission_keys"] == created["permission_keys"]
        assert copied["default_scope_type"] == created["default_scope_type"]
        assert copied["audits"][-1]["action"] == "role.copy"

        departments_response = await test_client.get("/api/departments", headers=admin_headers)
        assert departments_response.status_code == 200, departments_response.text
        department_id = departments_response.json()[0]["id"]

        update_response = await test_client.put(
            f"/api/roles/{created['id']}",
            json={
                "name": "测试角色已修改",
                "description": "权限和默认范围均已修改",
                "permission_keys": ["user:read"],
                "default_scope_type": "selected_organizations_and_descendants",
                "default_department_ids": [department_id],
            },
            headers=admin_headers,
        )
        assert update_response.status_code == 200, update_response.text
        updated = update_response.json()
        assert updated["permission_keys"] == ["user:read"]
        assert updated["default_department_ids"] == [department_id]
        audit = updated["audits"][-1]
        assert audit["action"] == "role.update"
        assert audit["before"]["permission_keys"] == ["role:read", "dashboard:view"]
        assert audit["after"]["permission_keys"] == ["user:read"]
        assert audit["before"]["default_scope_type"] == "self"
        assert audit["after"]["default_scope_type"] == "selected_organizations_and_descendants"

        deactivate_response = await test_client.post(
            f"/api/roles/{copied['id']}/deactivate",
            headers=admin_headers,
        )
        assert deactivate_response.status_code == 200, deactivate_response.text
        deactivated = deactivate_response.json()
        assert deactivated["is_active"] is False
        assert deactivated["permission_keys"] == ["role:read", "dashboard:view"]
        assert deactivated["audits"][-1]["action"] == "role.deactivate"

        overview_response = await test_client.get("/api/roles/overview", headers=admin_headers)
        assert overview_response.status_code == 200, overview_response.text
        overview_role = next(item for item in overview_response.json()["roles"] if item["id"] == created["id"])
        assert [item["action"] for item in overview_role["audits"]] == ["role.create", "role.update"]
    finally:
        await _cleanup_custom_roles([role_code, copy_code])


async def test_builtin_roles_are_protected_and_standard_users_cannot_manage_roles(
    test_client,
    role_test_users,
):
    """服务端必须独立保护角色定义，不能依赖前端隐藏按钮。"""

    admin_headers = role_test_users["admin_headers"]
    standard_user = role_test_users["standard"]
    overview_response = await test_client.get("/api/roles/overview", headers=admin_headers)
    assert overview_response.status_code == 200, overview_response.text
    builtin = next(item for item in overview_response.json()["roles"] if item["code"] == "user")

    forbidden_response = await test_client.post(
        "/api/roles",
        json={
            "code": f"pytest_forbidden_{uuid.uuid4().hex[:8]}",
            "name": "越权角色",
            "description": "",
            "permission_keys": [],
            "default_scope_type": "none",
            "default_department_ids": [],
        },
        headers=standard_user["headers"],
    )
    assert forbidden_response.status_code == 403

    update_response = await test_client.put(
        f"/api/roles/{builtin['id']}",
        json={
            "name": "被篡改的内置角色",
            "description": "",
            "permission_keys": [],
            "default_scope_type": "none",
            "default_department_ids": [],
        },
        headers=admin_headers,
    )
    assert update_response.status_code == 409
    assert "内置角色" in update_response.json()["detail"]

    deactivate_response = await test_client.post(
        f"/api/roles/{builtin['id']}/deactivate",
        headers=admin_headers,
    )
    assert deactivate_response.status_code == 409
    assert "内置角色" in deactivate_response.json()["detail"]


async def test_custom_role_with_active_member_cannot_be_deactivated(test_client, role_test_users):
    """仍有有效成员的自定义角色必须先迁移成员。"""

    admin_headers = role_test_users["admin_headers"]
    standard_user = role_test_users["standard"]
    role_code = f"pytest_member_role_{uuid.uuid4().hex[:10]}"
    role_id = None
    try:
        create_response = await test_client.post(
            "/api/roles",
            json={
                "code": role_code,
                "name": "测试占用角色",
                "description": "",
                "permission_keys": ["role:read"],
                "default_scope_type": "self",
                "default_department_ids": [],
            },
            headers=admin_headers,
        )
        assert create_response.status_code == 201, create_response.text
        role_id = create_response.json()["id"]

        async with pg_manager.get_async_session_context() as session:
            session.add(
                UserRoleAssignment(
                    user_id=standard_user["user"]["id"],
                    role_id=role_id,
                    scope_mode="inherit",
                )
            )

        response = await test_client.post(f"/api/roles/{role_id}/deactivate", headers=admin_headers)

        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "该角色仍有成员，请先迁移成员后再停用"
    finally:
        if role_id is not None:
            async with pg_manager.get_async_session_context() as session:
                await session.execute(delete(UserRoleAssignment).where(UserRoleAssignment.role_id == role_id))
        await _cleanup_custom_roles([role_code])
