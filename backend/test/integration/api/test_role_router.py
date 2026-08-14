"""角色与权限只读总览接口集成测试。"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import OperationLog, Role, SecurityAudit, User, UserRoleAssignment
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_superadmin_can_view_builtin_roles_permissions_scopes_and_members(test_client, admin_headers):
    profile_response = await test_client.get("/api/auth/me", headers=admin_headers)
    assert profile_response.status_code == 200, profile_response.text
    if "superadmin" not in {role["code"] for role in profile_response.json()["roles"]}:
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


async def test_role_read_permission_changes_take_effect_on_next_request(test_client, role_test_users):
    """同一令牌的下一次请求应读取最新角色权限。"""

    admin_headers = role_test_users["admin_headers"]
    standard = role_test_users["standard"]
    role_code = f"pytest_live_auth_{uuid.uuid4().hex[:10]}"
    delegated_role_code = f"pytest_delegated_manage_{uuid.uuid4().hex[:8]}"
    role_id = None
    user_role_id = None
    try:
        overview = await test_client.get("/api/roles/overview", headers=admin_headers)
        assert overview.status_code == 200, overview.text
        user_role_id = next(role["id"] for role in overview.json()["roles"] if role["code"] == "user")

        created = await test_client.post(
            "/api/roles",
            json={
                "code": role_code,
                "name": "实时授权测试角色",
                "description": "",
                "permission_keys": ["role:read", "role:manage"],
                "default_scope_type": "self",
                "default_department_ids": [],
            },
            headers=admin_headers,
        )
        assert created.status_code == 201, created.text
        role_id = created.json()["id"]

        assigned = await test_client.put(
            f"/api/auth/users/{standard['user']['id']}",
            json={"role_assignments": [{"role_id": role_id, "scope_mode": "inherit"}]},
            headers=admin_headers,
        )
        assert assigned.status_code == 200, assigned.text
        assert "effective_permissions" not in assigned.json()

        allowed = await test_client.get("/api/roles/overview", headers=standard["headers"])
        assert allowed.status_code == 200, allowed.text
        visible_member_ids = {member["id"] for role in allowed.json()["roles"] for member in role["members"]}
        assert visible_member_ids == {standard["user"]["id"]}
        allowed_role = next(role for role in allowed.json()["roles"] if role["id"] == role_id)
        assert allowed_role["audits"] == []
        profile = await test_client.get("/api/auth/me", headers=standard["headers"])
        assert profile.status_code == 200, profile.text
        assert profile.json()["effective_permissions"] == ["role:read", "role:manage"]

        delegated_manage = await test_client.post(
            "/api/roles",
            json={
                "code": delegated_role_code,
                "name": "权限管理域角色",
                "description": "",
                "permission_keys": [],
                "default_scope_type": "none",
                "default_department_ids": [],
            },
            headers=standard["headers"],
        )
        assert delegated_manage.status_code == 201, delegated_manage.text

        updated = await test_client.put(
            f"/api/roles/{role_id}",
            json={
                "name": "实时授权测试角色",
                "description": "",
                "permission_keys": [],
                "default_scope_type": "self",
                "default_department_ids": [],
            },
            headers=admin_headers,
        )
        assert updated.status_code == 200, updated.text

        denied = await test_client.get("/api/roles/overview", headers=standard["headers"])
        assert denied.status_code == 403, denied.text
        refreshed_profile = await test_client.get("/api/auth/me", headers=standard["headers"])
        assert refreshed_profile.status_code == 200, refreshed_profile.text
        assert refreshed_profile.json()["effective_permissions"] == []
    finally:
        if role_id is not None and user_role_id is not None:
            await test_client.put(
                f"/api/auth/users/{standard['user']['id']}",
                json={"role_assignments": [{"role_id": user_role_id, "scope_mode": "inherit"}]},
                headers=admin_headers,
            )
        await _cleanup_custom_roles([role_code, delegated_role_code])


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
            ),
            User(
                username=f"pytest-role-user-{suffix}",
                uid=f"pytest_role_user_{suffix}",
                password_hash=AuthUtils.hash_password(password),
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
            "admin": {"user": {"id": user_ids[0]}, "headers": headers[0]},
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
        overview = overview_response.json()
        overview_role = next(item for item in overview["roles"] if item["id"] == created["id"])
        assert {item["id"]: item["name"] for item in overview["scope_departments"]}[department_id] == (
            departments_response.json()[0]["name"]
        )
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


async def test_superadmin_can_assign_multiple_roles_with_narrower_scope(test_client, role_test_users):
    """用户响应应返回多角色，并保存不超过角色默认值的个性化范围。"""

    admin_headers = role_test_users["admin_headers"]
    overview_response = await test_client.get("/api/roles/overview", headers=admin_headers)
    assert overview_response.status_code == 200, overview_response.text
    roles = {role["code"]: role for role in overview_response.json()["roles"]}
    suffix = uuid.uuid4().hex[:8]
    password = f"Pw!{uuid.uuid4().hex}"
    target_id = None
    try:
        create_response = await test_client.post(
            "/api/auth/users",
            json={"username": f"urm_{suffix}", "password": password},
            headers=admin_headers,
        )
        assert create_response.status_code == 200, create_response.text
        created = create_response.json()
        target_id = created["id"]
        assert [role["code"] for role in created["roles"]] == ["user"]

        login_response = await test_client.post(
            "/api/auth/token",
            data={"username": created["uid"], "password": password},
        )
        assert login_response.status_code == 200, login_response.text
        target_headers = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

        response = await test_client.put(
            f"/api/auth/users/{target_id}",
            json={
                "role_assignments": [
                    {"role_id": roles["user"]["id"], "scope_mode": "inherit"},
                    {
                        "role_id": roles["admin"]["id"],
                        "scope_mode": "override",
                        "override_scope_type": "self",
                    },
                ]
            },
            headers=admin_headers,
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert "role" not in payload
        assert {role["code"] for role in payload["roles"]} == {"admin", "user"}
        admin_assignment = next(role for role in payload["roles"] if role["code"] == "admin")
        assert admin_assignment["scope_mode"] == "override"
        assert admin_assignment["effective_scope_type"] == "self"
        assert admin_assignment["override_department_ids"] == []

        profile_response = await test_client.get("/api/auth/me", headers=target_headers)
        assert profile_response.status_code == 200, profile_response.text
        assert {role["code"] for role in profile_response.json()["roles"]} == {"admin", "user"}

        async with pg_manager.get_async_session_context() as session:
            audit = await session.scalar(
                select(SecurityAudit)
                .where(SecurityAudit.target_type == "user", SecurityAudit.target_id == target_id)
                .order_by(SecurityAudit.id.desc())
            )
            assert audit is not None
            assert audit.action == "user.roles.update"
            assert {item["code"] for item in audit.after_value["roles"]} == {"admin", "user"}
    finally:
        if target_id is not None:
            delete_response = await test_client.delete(f"/api/auth/users/{target_id}", headers=admin_headers)
            assert delete_response.status_code in {200, 404}, delete_response.text


async def test_role_assignments_reject_expansion_and_protect_superadmin_changes(test_client, role_test_users):
    """覆盖范围不能扩宽，超级角色必须独占且授予撤销均要求原因。"""

    admin_headers = role_test_users["admin_headers"]
    standard = role_test_users["standard"]
    overview_response = await test_client.get("/api/roles/overview", headers=admin_headers)
    assert overview_response.status_code == 200, overview_response.text
    roles = {role["code"]: role for role in overview_response.json()["roles"]}
    endpoint = f"/api/auth/users/{standard['user']['id']}"

    expansion_response = await test_client.put(
        endpoint,
        json={
            "role_assignments": [
                {
                    "role_id": roles["user"]["id"],
                    "scope_mode": "override",
                    "override_scope_type": "all",
                }
            ]
        },
        headers=admin_headers,
    )
    assert expansion_response.status_code == 400, expansion_response.text
    assert "不能超过角色默认范围" in expansion_response.json()["detail"]

    mixed_response = await test_client.put(
        endpoint,
        json={
            "reason": "测试超级角色互斥",
            "role_assignments": [
                {"role_id": roles["superadmin"]["id"], "scope_mode": "inherit"},
                {"role_id": roles["user"]["id"], "scope_mode": "inherit"},
            ],
        },
        headers=admin_headers,
    )
    assert mixed_response.status_code == 400, mixed_response.text
    assert "不能与其他角色同时分配" in mixed_response.json()["detail"]

    missing_reason_response = await test_client.put(
        endpoint,
        json={"role_assignments": [{"role_id": roles["superadmin"]["id"], "scope_mode": "inherit"}]},
        headers=admin_headers,
    )
    assert missing_reason_response.status_code == 400, missing_reason_response.text
    assert "必须填写原因" in missing_reason_response.json()["detail"]

    grant_response = await test_client.put(
        endpoint,
        json={
            "reason": "轮值超级管理员",
            "role_assignments": [{"role_id": roles["superadmin"]["id"], "scope_mode": "inherit"}],
        },
        headers=admin_headers,
    )
    assert grant_response.status_code == 200, grant_response.text
    assert "role" not in grant_response.json()
    assert [role["code"] for role in grant_response.json()["roles"]] == ["superadmin"]

    revoke_without_reason = await test_client.put(
        endpoint,
        json={"role_assignments": [{"role_id": roles["user"]["id"], "scope_mode": "inherit"}]},
        headers=admin_headers,
    )
    assert revoke_without_reason.status_code == 400, revoke_without_reason.text
    assert "必须填写原因" in revoke_without_reason.json()["detail"]

    revoke_response = await test_client.put(
        endpoint,
        json={
            "reason": "轮值结束",
            "role_assignments": [{"role_id": roles["user"]["id"], "scope_mode": "inherit"}],
        },
        headers=admin_headers,
    )
    assert revoke_response.status_code == 200, revoke_response.text
    assert "role" not in revoke_response.json()

    async with pg_manager.get_async_session_context() as session:
        audits = list(
            (
                await session.scalars(
                    select(SecurityAudit)
                    .where(SecurityAudit.target_type == "user", SecurityAudit.target_id == standard["user"]["id"])
                    .order_by(SecurityAudit.id.asc())
                )
            ).all()
        )
        assert [audit.reason for audit in audits[-2:]] == ["轮值超级管理员", "轮值结束"]


async def test_last_active_superadmin_assignment_cannot_be_removed(test_client, role_test_users):
    """通过真实接口验证系统始终保留一个有效超级管理员。"""

    admin = role_test_users["admin"]
    overview_response = await test_client.get("/api/roles/overview", headers=admin["headers"])
    assert overview_response.status_code == 200, overview_response.text
    user_role = next(role for role in overview_response.json()["roles"] if role["code"] == "user")

    async with pg_manager.get_async_session_context() as session:
        other_superadmin_ids = list(
            (
                await session.scalars(
                    select(User.id)
                    .join(UserRoleAssignment)
                    .join(Role)
                    .where(
                        Role.code == "superadmin",
                        User.is_deleted == 0,
                        User.id != admin["user"]["id"],
                    )
                )
            ).all()
        )
        if other_superadmin_ids:
            await session.execute(update(User).where(User.id.in_(other_superadmin_ids)).values(is_deleted=1))

    try:
        response = await test_client.put(
            f"/api/auth/users/{admin['user']['id']}",
            json={
                "reason": "测试最后一个超级管理员保护",
                "role_assignments": [{"role_id": user_role["id"], "scope_mode": "inherit"}],
            },
            headers=admin["headers"],
        )

        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "系统必须至少保留一个有效超级管理员"
    finally:
        if other_superadmin_ids:
            async with pg_manager.get_async_session_context() as session:
                await session.execute(update(User).where(User.id.in_(other_superadmin_ids)).values(is_deleted=0))
