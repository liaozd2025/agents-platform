"""
Integration tests for authentication-related API routes.
"""

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
    RoleDefaultDepartment,
    RolePermission,
    SecurityAudit,
    User,
    UserRoleAssignment,
)
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def user_management_test_users(test_client):
    """创建不依赖外部凭据的用户管理测试账号。"""

    pg_manager.initialize()
    await pg_manager.async_engine.dispose()
    await pg_manager.create_tables()
    await pg_manager.ensure_business_schema()

    suffix = uuid.uuid4().hex[:10]
    password = f"Pw!{uuid.uuid4().hex}"
    async with pg_manager.get_async_session_context() as session:
        root = await session.get(Department, ROOT_DEPARTMENT_ID)
        assert root is not None, "用户管理集成测试需要现有集团根节点"
        roles = {
            role.code: role
            for role in (await session.scalars(select(Role).where(Role.code.in_(("superadmin", "user"))))).all()
        }
        users = [
            User(
                username=f"pytest-user-admin-{suffix}",
                uid=f"pytest_user_admin_{suffix}",
                password_hash=AuthUtils.hash_password(password),
                role="superadmin",
                department_id=root.id,
            ),
            User(
                username=f"pytest-user-standard-{suffix}",
                uid=f"pytest_user_standard_{suffix}",
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

    headers = []
    for user in users:
        response = await test_client.post("/api/auth/token", data={"username": user.uid, "password": password})
        assert response.status_code == 200, response.text
        headers.append({"Authorization": f"Bearer {response.json()['access_token']}"})

    try:
        yield {"admin_headers": headers[0], "standard_headers": headers[1]}
    finally:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(delete(SecurityAudit).where(SecurityAudit.actor_user_id.in_(user_ids)))
            await session.execute(delete(OperationLog).where(OperationLog.user_id.in_(user_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await pg_manager.async_engine.dispose()


async def _require_superadmin(test_client, headers):
    response = await test_client.get("/api/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    if response.json()["role"] != "superadmin":
        pytest.fail("This test requires TEST_USERNAME to be a superadmin account.")


async def _create_department_with_admin(test_client, headers, label: str) -> dict:
    suffix = uuid.uuid4().hex[:8]
    admin_uid = f"adm{label}_{suffix}"
    admin_password = f"Pw!{suffix}"
    response = await test_client.post(
        "/api/departments",
        json={
            "name": f"pytest_{label}_{suffix}",
            "description": "pytest managed department",
            "admin_uid": admin_uid,
            "admin_password": admin_password,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text

    login_response = await test_client.post(
        "/api/auth/token",
        data={"username": admin_uid, "password": admin_password},
    )
    assert login_response.status_code == 200, login_response.text

    login_payload = login_response.json()
    return {
        "department": response.json(),
        "admin_id": login_payload["user_id"],
        "admin_headers": {"Authorization": f"Bearer {login_payload['access_token']}"},
    }


async def _create_department(test_client, headers, label: str, parent_id: int) -> dict:
    """在指定父节点下创建一个测试组织节点。"""

    response = await test_client.post(
        "/api/departments",
        json={
            "name": f"pytest_{label}_{uuid.uuid4().hex[:8]}",
            "description": "pytest managed department",
            "parent_id": parent_id,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_custom_user(
    test_client,
    department_id: int,
    *,
    permission_keys: tuple[str, ...],
    scope_type: str = "selected_organizations_and_descendants",
) -> dict:
    """创建具有指定权限和数据范围的测试用户。"""

    suffix = uuid.uuid4().hex[:10]
    password = f"Pw!{uuid.uuid4().hex}"
    async with pg_manager.get_async_session_context() as session:
        role = Role(
            code=f"pytest_user_scope_{suffix}",
            name="用户权限测试角色",
            description="",
            is_builtin=False,
            is_active=True,
            default_scope_type=scope_type,
            permissions=[RolePermission(permission_key=key) for key in permission_keys],
            default_departments=(
                [RoleDefaultDepartment(department_id=department_id)]
                if scope_type == "selected_organizations_and_descendants"
                else []
            ),
        )
        user = User(
            username=f"pytest-user-scope-{suffix}",
            uid=f"pytest_user_scope_{suffix}",
            password_hash=AuthUtils.hash_password(password),
            role="user",
            department_id=department_id,
        )
        session.add_all([role, user])
        await session.flush()
        session.add(UserRoleAssignment(user=user, role=role, scope_mode="inherit"))
        user_id = user.id
        role_id = role.id

    response = await test_client.post("/api/auth/token", data={"username": user.uid, "password": password})
    assert response.status_code == 200, response.text
    return {
        "user_id": user_id,
        "role_id": role_id,
        "headers": {"Authorization": f"Bearer {response.json()['access_token']}"},
    }


async def _cleanup_custom_user(user_id: int, role_id: int) -> None:
    """清理自定义角色及其用户。"""

    async with pg_manager.get_async_session_context() as session:
        await session.execute(delete(SecurityAudit).where(SecurityAudit.actor_user_id == user_id))
        await session.execute(delete(OperationLog).where(OperationLog.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.execute(delete(Role).where(Role.id == role_id))


async def _create_user(test_client, headers, label: str, role: str = "user", department_id: int | None = None) -> dict:
    suffix = uuid.uuid4().hex[:8]
    payload = {
        "username": f"u{label}_{suffix}",
        "password": f"Pw!{suffix}",
        "role": role,
    }
    if department_id is not None:
        payload["department_id"] = department_id

    response = await test_client.post("/api/auth/users", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _cleanup_user(test_client, headers, user_id: int) -> None:
    response = await test_client.delete(f"/api/auth/users/{user_id}", headers=headers)
    assert response.status_code in {200, 404}, response.text


async def _cleanup_department(test_client, headers, department_id: int) -> None:
    response = await test_client.delete(f"/api/departments/{department_id}", headers=headers)
    assert response.status_code in {200, 404}, response.text


async def test_login_with_invalid_credentials(test_client):
    response = await test_client.post("/api/auth/token", data={"username": "invalid", "password": "invalid"})
    assert response.status_code == 401
    assert "detail" in response.json()


async def test_user_is_locked_after_repeated_failed_logins(test_client, standard_user):
    uid = standard_user["user"]["uid"]

    for attempt in range(1, 5):
        response = await test_client.post("/api/auth/token", data={"username": uid, "password": "wrong-password"})
        assert response.status_code == 401, response.text
        assert response.json()["detail"] == "用户名或密码错误"

    locked_response = await test_client.post("/api/auth/token", data={"username": uid, "password": "wrong-password"})
    assert locked_response.status_code == 423, locked_response.text
    assert "X-Lock-Remaining" in locked_response.headers
    assert "账户已被锁定" in locked_response.json()["detail"]

    still_locked_response = await test_client.post(
        "/api/auth/token",
        data={"username": uid, "password": standard_user["password"]},
    )
    assert still_locked_response.status_code == 423, still_locked_response.text
    assert "X-Lock-Remaining" in still_locked_response.headers
    assert "登录被锁定" in still_locked_response.json()["detail"]


async def test_admin_can_login_and_fetch_profile(test_client, admin_headers):
    profile_response = await test_client.get("/api/auth/me", headers=admin_headers)
    assert profile_response.status_code == 200
    data = profile_response.json()
    assert data["role"] in {"admin", "superadmin"}
    assert data["username"]
    assert data["id"]
    assert data["roles"]
    assert "role:read" in data["effective_permissions"]


async def test_profile_requires_authentication(test_client):
    response = await test_client.get("/api/auth/me")
    assert response.status_code == 401
    assert response.json()["detail"] == "请登录后再访问"


async def test_admin_can_create_and_delete_user(test_client, admin_headers):
    suffix = uuid.uuid4().hex[:8]
    payload = {
        "username": f"rtu_{suffix}",
        "password": "routerTest123!",
        "role": "user",
    }
    create_response = await test_client.post("/api/auth/users", json=payload, headers=admin_headers)
    assert create_response.status_code == 200, create_response.text

    created_user = create_response.json()
    assert created_user["username"] == payload["username"]
    assert created_user["role"] == payload["role"]
    assert [role["code"] for role in created_user["roles"]] == ["user"]

    delete_response = await test_client.delete(f"/api/auth/users/{created_user['id']}", headers=admin_headers)
    assert delete_response.status_code == 200, delete_response.text
    delete_payload = delete_response.json()
    assert delete_payload["success"] is True
    assert delete_payload["message"] == "用户已删除"


async def test_admin_password_mutations_reject_passwords_shorter_than_eight_characters(
    test_client, admin_headers, standard_user
):
    create_response = await test_client.post(
        "/api/auth/users",
        json={"username": f"weak_{uuid.uuid4().hex[:8]}", "password": "short", "role": "user"},
        headers=admin_headers,
    )
    assert create_response.status_code == 422, create_response.text
    assert create_response.json()["detail"][0]["loc"] == ["body", "password"]

    update_response = await test_client.put(
        f"/api/auth/users/{standard_user['user']['id']}",
        json={"password": "short"},
        headers=admin_headers,
    )
    assert update_response.status_code == 422, update_response.text
    assert update_response.json()["detail"][0]["loc"] == ["body", "password"]


async def test_user_management_follows_authorized_organization_subtree(test_client, user_management_test_users):
    """用户管理应覆盖授权子树、隔离兄弟组织并保留直属查询。"""

    admin_headers = user_management_test_users["admin_headers"]

    user_ids: list[int] = []
    admin_ids: list[int] = []
    department_ids: list[int] = []
    custom_reader = None
    self_updater = None

    try:
        dept_a = await _create_department_with_admin(test_client, admin_headers, "a")
        dept_b = await _create_department_with_admin(test_client, admin_headers, "b")
        department_a = dept_a["department"]
        department_b = dept_b["department"]
        child_department = await _create_department(test_client, admin_headers, "a_child", department_a["id"])
        department_ids.extend([child_department["id"], department_a["id"], department_b["id"]])
        admin_ids.extend([dept_a["admin_id"], dept_b["admin_id"]])

        user_a = await _create_user(test_client, dept_a["admin_headers"], "a")
        user_ids.append(user_a["id"])
        backup_admin = await _create_user(
            test_client,
            admin_headers,
            "a_admin",
            role="admin",
            department_id=department_a["id"],
        )
        user_ids.append(backup_admin["id"])
        child_user = await _create_user(
            test_client,
            dept_a["admin_headers"],
            "a_child",
            department_id=child_department["id"],
        )
        user_ids.append(child_user["id"])
        user_b = await _create_user(test_client, dept_b["admin_headers"], "b")
        user_ids.append(user_b["id"])
        superadmin_created_user = await _create_user(test_client, admin_headers, "s", department_id=department_b["id"])
        user_ids.append(superadmin_created_user["id"])
        custom_reader = await _create_custom_user(
            test_client,
            department_a["id"],
            permission_keys=("user:read", "department:read"),
        )
        self_updater = await _create_custom_user(
            test_client,
            department_a["id"],
            permission_keys=("user:update",),
            scope_type="self",
        )

        assert user_a["department_id"] == department_a["id"]
        assert child_user["department_id"] == child_department["id"]
        assert superadmin_created_user["department_id"] == department_b["id"]

        forbidden_create = await test_client.post(
            "/api/auth/users",
            json={
                "username": f"ux_{uuid.uuid4().hex[:8]}",
                "password": "routerTest123!",
                "role": "user",
                "department_id": department_b["id"],
            },
            headers=dept_a["admin_headers"],
        )
        assert forbidden_create.status_code == 404, forbidden_create.text

        list_response = await test_client.get("/api/auth/users", headers=dept_a["admin_headers"])
        assert list_response.status_code == 200, list_response.text
        listed_users = list_response.json()
        listed_user_ids = {user["id"] for user in listed_users}
        assert user_a["id"] in listed_user_ids
        assert child_user["id"] in listed_user_ids
        assert user_b["id"] not in listed_user_ids

        subtree_response = await test_client.get(
            f"/api/auth/users?department_id={department_a['id']}&limit=1000",
            headers=dept_a["admin_headers"],
        )
        assert subtree_response.status_code == 200, subtree_response.text
        subtree_ids = {user["id"] for user in subtree_response.json()}
        assert {user_a["id"], child_user["id"]}.issubset(subtree_ids)
        assert user_b["id"] not in subtree_ids

        direct_response = await test_client.get(
            f"/api/auth/users?department_id={department_a['id']}&direct=true&limit=1000",
            headers=dept_a["admin_headers"],
        )
        assert direct_response.status_code == 200, direct_response.text
        direct_ids = {user["id"] for user in direct_response.json()}
        assert user_a["id"] in direct_ids
        assert child_user["id"] not in direct_ids

        options_response = await test_client.get(
            f"/api/auth/users/access-options?department_id={department_a['id']}",
            headers=dept_a["admin_headers"],
        )
        assert options_response.status_code == 200, options_response.text
        access_options = options_response.json()
        option_uids = {user["uid"] for user in access_options}
        assert user_a["uid"] in option_uids
        assert child_user["uid"] in option_uids
        assert user_b["uid"] not in option_uids

        out_of_scope_list = await test_client.get(
            f"/api/auth/users?department_id={department_b['id']}",
            headers=dept_a["admin_headers"],
        )
        assert out_of_scope_list.status_code == 404, out_of_scope_list.text
        out_of_scope_options = await test_client.get(
            f"/api/auth/users/access-options?department_id={department_b['id']}",
            headers=dept_a["admin_headers"],
        )
        assert out_of_scope_options.status_code == 404, out_of_scope_options.text

        custom_list = await test_client.get("/api/auth/users?limit=1000", headers=custom_reader["headers"])
        assert custom_list.status_code == 200, custom_list.text
        custom_user_ids = {user["id"] for user in custom_list.json()}
        assert {user_a["id"], child_user["id"], custom_reader["user_id"]}.issubset(custom_user_ids)
        assert user_b["id"] not in custom_user_ids

        custom_tree = await test_client.get("/api/departments", headers=custom_reader["headers"])
        assert custom_tree.status_code == 200, custom_tree.text
        custom_department_ids = {department["id"] for department in custom_tree.json()}
        assert {department_a["parent_id"], department_a["id"], child_department["id"]}.issubset(custom_department_ids)
        assert department_b["id"] not in custom_department_ids

        custom_create = await test_client.post(
            "/api/auth/users",
            json={"username": f"readonly_{uuid.uuid4().hex[:8]}", "password": "routerTest123!"},
            headers=custom_reader["headers"],
        )
        assert custom_create.status_code == 403, custom_create.text

        self_update = await test_client.put(
            f"/api/auth/users/{self_updater['user_id']}",
            json={"username": f"self_{uuid.uuid4().hex[:8]}"},
            headers=self_updater["headers"],
        )
        assert self_update.status_code == 200, self_update.text
        self_move_outside_scope = await test_client.put(
            f"/api/auth/users/{self_updater['user_id']}",
            json={"department_id": department_b["id"]},
            headers=self_updater["headers"],
        )
        assert self_move_outside_scope.status_code == 404, self_move_outside_scope.text

        tree_response = await test_client.get("/api/departments", headers=dept_a["admin_headers"])
        assert tree_response.status_code == 200, tree_response.text
        tree = {department["id"]: department for department in tree_response.json()}
        assert {department_a["parent_id"], department_a["id"], child_department["id"]}.issubset(tree)
        assert department_b["id"] not in tree
        assert tree[department_a["id"]]["user_count"] == 5
        assert tree[child_department["id"]]["user_count"] == 1

        new_child = await _create_department(test_client, admin_headers, "a_new", department_a["id"])
        department_ids.insert(0, new_child["id"])
        refreshed_tree = await test_client.get("/api/departments", headers=dept_a["admin_headers"])
        assert refreshed_tree.status_code == 200, refreshed_tree.text
        assert new_child["id"] in {department["id"] for department in refreshed_tree.json()}

        superadmin_list_response = await test_client.get("/api/auth/users?limit=1000", headers=admin_headers)
        assert superadmin_list_response.status_code == 200, superadmin_list_response.text
        superadmin_user_ids = {user["id"] for user in superadmin_list_response.json()}
        assert user_a["id"] in superadmin_user_ids
        assert child_user["id"] in superadmin_user_ids
        assert user_b["id"] in superadmin_user_ids

        child_read = await test_client.get(f"/api/auth/users/{child_user['id']}", headers=dept_a["admin_headers"])
        assert child_read.status_code == 200, child_read.text

        cross_read = await test_client.get(f"/api/auth/users/{user_b['id']}", headers=dept_a["admin_headers"])
        assert cross_read.status_code == 404, cross_read.text

        cross_update = await test_client.put(
            f"/api/auth/users/{user_b['id']}",
            json={"username": f"ub_{uuid.uuid4().hex[:8]}"},
            headers=dept_a["admin_headers"],
        )
        assert cross_update.status_code == 404, cross_update.text

        child_update = await test_client.put(
            f"/api/auth/users/{child_user['id']}",
            json={"username": f"child_{uuid.uuid4().hex[:8]}"},
            headers=dept_a["admin_headers"],
        )
        assert child_update.status_code == 200, child_update.text

        move_within_scope = await test_client.put(
            f"/api/auth/users/{user_a['id']}",
            json={"department_id": child_department["id"]},
            headers=dept_a["admin_headers"],
        )
        assert move_within_scope.status_code == 200, move_within_scope.text
        assert move_within_scope.json()["department_id"] == child_department["id"]

        move_outside_scope = await test_client.put(
            f"/api/auth/users/{user_a['id']}",
            json={"department_id": department_b["id"]},
            headers=dept_a["admin_headers"],
        )
        assert move_outside_scope.status_code == 404, move_outside_scope.text

        role_escalation = await test_client.put(
            f"/api/auth/users/{user_a['id']}", json={"role": "admin"}, headers=dept_a["admin_headers"]
        )
        assert role_escalation.status_code == 422, role_escalation.text

        cross_delete = await test_client.delete(f"/api/auth/users/{user_b['id']}", headers=dept_a["admin_headers"])
        assert cross_delete.status_code == 404, cross_delete.text

        child_delete = await test_client.delete(f"/api/auth/users/{child_user['id']}", headers=dept_a["admin_headers"])
        assert child_delete.status_code == 200, child_delete.text
        user_ids.remove(child_user["id"])

        missing_user_permission = await test_client.get(
            "/api/auth/users", headers=user_management_test_users["standard_headers"]
        )
        assert missing_user_permission.status_code == 403, missing_user_permission.text
        missing_department_permission = await test_client.get(
            "/api/departments", headers=user_management_test_users["standard_headers"]
        )
        assert missing_department_permission.status_code == 403, missing_department_permission.text

        move_admin_with_backup = await test_client.put(
            f"/api/auth/users/{dept_a['admin_id']}",
            json={"department_id": child_department["id"]},
            headers=admin_headers,
        )
        assert move_admin_with_backup.status_code == 200, move_admin_with_backup.text
    finally:
        if custom_reader is not None:
            await _cleanup_custom_user(custom_reader["user_id"], custom_reader["role_id"])
        if self_updater is not None:
            await _cleanup_custom_user(self_updater["user_id"], self_updater["role_id"])
        for user_id in user_ids:
            await _cleanup_user(test_client, admin_headers, user_id)
        for admin_id in admin_ids:
            await _cleanup_user(test_client, admin_headers, admin_id)
        for department_id in department_ids:
            await _cleanup_department(test_client, admin_headers, department_id)


async def test_invalid_token_is_rejected(test_client):
    headers = {"Authorization": "Bearer not-a-real-token"}
    response = await test_client.get("/api/auth/me", headers=headers)
    assert response.status_code == 401


async def test_deleted_user_token_is_rejected(test_client, admin_headers, standard_user):
    user_id = standard_user["user"]["id"]

    delete_response = await test_client.delete(f"/api/auth/users/{user_id}", headers=admin_headers)
    assert delete_response.status_code == 200, delete_response.text

    profile_response = await test_client.get("/api/auth/me", headers=standard_user["headers"])
    assert profile_response.status_code == 401


async def test_locked_user_token_is_rejected(test_client, standard_user):
    uid = standard_user["user"]["uid"]

    for _ in range(5):
        await test_client.post("/api/auth/token", data={"username": uid, "password": "wrong-password"})

    profile_response = await test_client.get("/api/auth/me", headers=standard_user["headers"])
    assert profile_response.status_code == 423
    assert "X-Lock-Remaining" in profile_response.headers
