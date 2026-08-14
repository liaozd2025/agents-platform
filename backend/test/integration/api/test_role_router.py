"""角色与权限只读总览接口集成测试。"""

from __future__ import annotations

import pytest

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
