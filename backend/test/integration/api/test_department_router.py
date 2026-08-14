"""
Integration tests for department management API routes.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ROOT_DEPARTMENT_ID = 1


async def _create_node(test_client, admin_headers, name, *, parent_id=None, node_type=None):
    """创建组织节点并返回响应，供树形结构用例复用。"""
    payload = {"name": name, "description": "integration test org node"}
    if parent_id is not None:
        payload["parent_id"] = parent_id
    if node_type is not None:
        payload["node_type"] = node_type
    return await test_client.post("/api/departments", json=payload, headers=admin_headers)


async def _create_node_id(test_client, admin_headers, name, *, parent_id=None, node_type=None):
    """创建组织节点并断言成功，返回其 ID。"""
    response = await _create_node(test_client, admin_headers, name, parent_id=parent_id, node_type=node_type)
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest_asyncio.fixture
async def created_node_ids(test_client, admin_headers):
    """记录用例创建的组织节点，并在用例结束后按相反顺序清理。"""
    node_ids = []
    yield node_ids
    for node_id in reversed(node_ids):
        await test_client.delete(f"/api/departments/{node_id}", headers=admin_headers)


async def test_superadmin_can_build_multi_level_tree(test_client, admin_headers, created_node_ids):
    suffix = uuid.uuid4().hex[:8]
    company_response = await _create_node(test_client, admin_headers, f"pytest_company_{suffix}", node_type="company")
    assert company_response.status_code == 201, company_response.text
    company = company_response.json()
    company_id = company["id"]
    created_node_ids.append(company_id)
    assert company["parent_id"] == ROOT_DEPARTMENT_ID
    assert company["node_type"] == "company"

    department_response = await _create_node(test_client, admin_headers, f"pytest_dept_{suffix}", parent_id=company_id)
    assert department_response.status_code == 201, department_response.text
    department = department_response.json()
    department_id = department["id"]
    created_node_ids.append(department_id)
    assert department["parent_id"] == company_id
    assert department["node_type"] == "department"

    list_response = await test_client.get("/api/departments", headers=admin_headers)
    assert list_response.status_code == 200, list_response.text
    listed = list_response.json()
    nodes = {node["id"]: node for node in listed}

    assert nodes[ROOT_DEPARTMENT_ID]["parent_id"] is None
    assert nodes[ROOT_DEPARTMENT_ID]["node_type"] == "group"
    assert nodes[company_id]["parent_id"] == ROOT_DEPARTMENT_ID
    assert nodes[department_id]["parent_id"] == company_id

    # 列表按祖先链排序，父节点必定先于子节点出现；祖先链算错时这里会红
    order = [node["id"] for node in listed]
    assert order.index(ROOT_DEPARTMENT_ID) < order.index(company_id) < order.index(department_id)


async def test_superadmin_can_move_subtree(test_client, admin_headers, created_node_ids):
    """移动组织节点时，其整棵子树仍按新祖先链连续展示。"""
    suffix = uuid.uuid4().hex[:8]
    first_company_id = await _create_node_id(test_client, admin_headers, f"pytest_move_a_{suffix}", node_type="company")
    created_node_ids.append(first_company_id)
    second_company_id = await _create_node_id(
        test_client, admin_headers, f"pytest_move_b_{suffix}", node_type="company"
    )
    created_node_ids.append(second_company_id)
    division_id = await _create_node_id(
        test_client, admin_headers, f"pytest_division_{suffix}", parent_id=first_company_id
    )
    created_node_ids.append(division_id)
    department_id = await _create_node_id(test_client, admin_headers, f"pytest_team_{suffix}", parent_id=division_id)
    created_node_ids.append(department_id)

    move_response = await test_client.put(
        f"/api/departments/{division_id}",
        json={"parent_id": second_company_id},
        headers=admin_headers,
    )
    assert move_response.status_code == 200, move_response.text
    assert move_response.json()["parent_id"] == second_company_id

    list_response = await test_client.get("/api/departments", headers=admin_headers)
    assert list_response.status_code == 200, list_response.text
    listed = list_response.json()
    nodes = {node["id"]: node for node in listed}
    order = [node["id"] for node in listed]

    assert nodes[division_id]["parent_id"] == second_company_id
    assert nodes[department_id]["parent_id"] == division_id
    assert order.index(second_company_id) < order.index(division_id) < order.index(department_id)


async def test_move_rejects_self_and_descendant_parent(test_client, admin_headers, created_node_ids):
    """组织节点不能移动到自身或自身后代之下。"""
    suffix = uuid.uuid4().hex[:8]
    parent_id = await _create_node_id(test_client, admin_headers, f"pytest_cycle_parent_{suffix}")
    created_node_ids.append(parent_id)
    child_id = await _create_node_id(test_client, admin_headers, f"pytest_cycle_child_{suffix}", parent_id=parent_id)
    created_node_ids.append(child_id)

    self_response = await test_client.put(
        f"/api/departments/{parent_id}", json={"parent_id": parent_id}, headers=admin_headers
    )
    assert self_response.status_code == 400, self_response.text
    assert self_response.json()["detail"] == "组织节点不能移动到自身之下"

    descendant_response = await test_client.put(
        f"/api/departments/{parent_id}", json={"parent_id": child_id}, headers=admin_headers
    )
    assert descendant_response.status_code == 400, descendant_response.text
    assert descendant_response.json()["detail"] == "组织节点不能移动到自身后代之下"


async def test_move_rejects_empty_and_missing_parent(test_client, admin_headers, created_node_ids):
    """移动请求必须指定一个实际存在的父节点。"""
    suffix = uuid.uuid4().hex[:8]
    node_id = await _create_node_id(test_client, admin_headers, f"pytest_move_target_{suffix}")
    created_node_ids.append(node_id)

    empty_response = await test_client.put(
        f"/api/departments/{node_id}", json={"parent_id": None}, headers=admin_headers
    )
    assert empty_response.status_code == 400, empty_response.text
    assert empty_response.json()["detail"] == "父级组织节点不能为空"

    missing_response = await test_client.put(
        f"/api/departments/{node_id}", json={"parent_id": 2_147_483_647}, headers=admin_headers
    )
    assert missing_response.status_code == 400, missing_response.text
    assert missing_response.json()["detail"] == "父级组织节点不存在"


async def test_move_rejects_group_root(test_client, admin_headers, created_node_ids):
    """集团根不可移动。"""
    suffix = uuid.uuid4().hex[:8]
    target_id = await _create_node_id(test_client, admin_headers, f"pytest_root_target_{suffix}")
    created_node_ids.append(target_id)

    response = await test_client.put(
        f"/api/departments/{ROOT_DEPARTMENT_ID}", json={"parent_id": target_id}, headers=admin_headers
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "集团根不允许移动"


async def test_move_rejects_duplicate_name_under_target_parent(test_client, admin_headers, created_node_ids):
    """移动不会在目标父节点下制造同名组织节点。"""
    suffix = uuid.uuid4().hex[:8]
    shared_name = f"pytest_move_dup_{suffix}"
    first_company_id = await _create_node_id(test_client, admin_headers, f"pytest_dup_a_{suffix}")
    created_node_ids.append(first_company_id)
    second_company_id = await _create_node_id(test_client, admin_headers, f"pytest_dup_b_{suffix}")
    created_node_ids.append(second_company_id)
    source_id = await _create_node_id(test_client, admin_headers, shared_name, parent_id=first_company_id)
    created_node_ids.append(source_id)
    existing_id = await _create_node_id(test_client, admin_headers, shared_name, parent_id=second_company_id)
    created_node_ids.append(existing_id)

    response = await test_client.put(
        f"/api/departments/{source_id}", json={"parent_id": second_company_id}, headers=admin_headers
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "同一父级下已存在同名组织节点"


async def test_same_name_is_allowed_under_different_parents(test_client, admin_headers, created_node_ids):
    suffix = uuid.uuid4().hex[:8]
    shared_name = f"pytest_hr_{suffix}"
    first_company_id = await _create_node_id(test_client, admin_headers, f"pytest_co_a_{suffix}", node_type="company")
    created_node_ids.append(first_company_id)
    second_company_id = await _create_node_id(test_client, admin_headers, f"pytest_co_b_{suffix}", node_type="company")
    created_node_ids.append(second_company_id)

    first_hr_response = await _create_node(test_client, admin_headers, shared_name, parent_id=first_company_id)
    assert first_hr_response.status_code == 201, first_hr_response.text
    first_hr_id = first_hr_response.json()["id"]
    created_node_ids.append(first_hr_id)

    second_hr_response = await _create_node(test_client, admin_headers, shared_name, parent_id=second_company_id)
    assert second_hr_response.status_code == 201, second_hr_response.text
    second_hr_id = second_hr_response.json()["id"]
    created_node_ids.append(second_hr_id)

    assert first_hr_id != second_hr_id


async def test_duplicate_name_under_same_parent_is_rejected(test_client, admin_headers, created_node_ids):
    suffix = uuid.uuid4().hex[:8]
    shared_name = f"pytest_dup_{suffix}"
    company_id = await _create_node_id(test_client, admin_headers, f"pytest_co_{suffix}", node_type="company")
    created_node_ids.append(company_id)

    first_child_response = await _create_node(test_client, admin_headers, shared_name, parent_id=company_id)
    assert first_child_response.status_code == 201, first_child_response.text
    first_child_id = first_child_response.json()["id"]
    created_node_ids.append(first_child_id)

    duplicate_response = await _create_node(test_client, admin_headers, shared_name, parent_id=company_id)
    assert duplicate_response.status_code == 400, duplicate_response.text
    assert duplicate_response.json()["detail"] == "同一父级下已存在同名组织节点"


async def test_create_department_without_admin_account(test_client, admin_headers, created_node_ids):
    suffix = uuid.uuid4().hex[:8]
    response = await _create_node(test_client, admin_headers, f"pytest_no_admin_{suffix}")
    assert response.status_code == 201, response.text
    body = response.json()
    created_node_ids.append(body["id"])
    assert body["user_count"] == 0


async def test_department_list_counts_active_users_only(test_client, admin_headers):
    """组织列表的聚合计数应与实际有效用户数一致。"""

    suffix = uuid.uuid4().hex[:8]
    department_id = None
    user_ids = []

    try:
        department_id = await _create_node_id(test_client, admin_headers, f"pytest_count_{suffix}")
        for index in range(2):
            response = await test_client.post(
                "/api/auth/users",
                json={
                    "username": f"cu_{suffix}_{index}",
                    "password": "RouterUser123!",
                    "department_id": department_id,
                },
                headers=admin_headers,
            )
            assert response.status_code == 200, response.text
            user_ids.append(response.json()["id"])

        list_response = await test_client.get("/api/departments", headers=admin_headers)
        assert list_response.status_code == 200, list_response.text
        listed = {department["id"]: department for department in list_response.json()}
        assert listed[department_id]["user_count"] == 2

        delete_response = await test_client.delete(f"/api/auth/users/{user_ids.pop()}", headers=admin_headers)
        assert delete_response.status_code == 200, delete_response.text

        list_response = await test_client.get("/api/departments", headers=admin_headers)
        assert list_response.status_code == 200, list_response.text
        listed = {department["id"]: department for department in list_response.json()}
        assert listed[department_id]["user_count"] == 1
    finally:
        for user_id in user_ids:
            await test_client.delete(f"/api/auth/users/{user_id}", headers=admin_headers)
        if department_id is not None:
            await test_client.delete(f"/api/departments/{department_id}", headers=admin_headers)


async def test_create_department_rejects_admin_phone_without_uid(test_client, admin_headers):
    """只填写管理员手机号时应明确拒绝，不能静默忽略该字段。"""
    suffix = uuid.uuid4().hex[:8]
    response = await test_client.post(
        "/api/departments",
        json={
            "name": f"pytest_phone_only_{suffix}",
            "description": "integration test org node",
            "admin_phone": "13800000000",
        },
        headers=admin_headers,
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "创建管理员时必须提供用户ID"


async def test_delete_department_with_children_is_rejected(test_client, admin_headers, created_node_ids):
    suffix = uuid.uuid4().hex[:8]
    parent_id = await _create_node_id(test_client, admin_headers, f"pytest_parent_{suffix}", node_type="company")
    created_node_ids.append(parent_id)
    child_id = await _create_node_id(test_client, admin_headers, f"pytest_child_{suffix}", parent_id=parent_id)
    created_node_ids.append(child_id)

    delete_response = await test_client.delete(f"/api/departments/{parent_id}", headers=admin_headers)
    assert delete_response.status_code == 400, delete_response.text
    assert "子节点" in delete_response.json()["detail"]

    still_exists = await test_client.get(f"/api/departments/{parent_id}", headers=admin_headers)
    assert still_exists.status_code == 200, still_exists.text

    child_still_exists = await test_client.get(f"/api/departments/{child_id}", headers=admin_headers)
    assert child_still_exists.status_code == 200, child_still_exists.text
    assert child_still_exists.json()["parent_id"] == parent_id


async def test_delete_department_with_direct_users_is_rejected(test_client, admin_headers):
    suffix = uuid.uuid4().hex[:8]
    department_payload = {
        "name": f"pytest_department_{suffix}",
        "description": "integration test department",
        "admin_uid": f"pta_{suffix}",
        "admin_password": "RouterDept123!",
    }
    user_payload = {
        "username": f"dept_user_{suffix}",
        "password": "RouterUser123!",
    }

    department_id = None
    created_user_id = None
    department_admin_id = None

    try:
        create_department_response = await test_client.post(
            "/api/departments",
            json=department_payload,
            headers=admin_headers,
        )
        assert create_department_response.status_code == 201, create_department_response.text
        department_id = create_department_response.json()["id"]

        create_user_response = await test_client.post(
            "/api/auth/users",
            json={**user_payload, "department_id": department_id},
            headers=admin_headers,
        )
        assert create_user_response.status_code == 200, create_user_response.text
        created_user_id = create_user_response.json()["id"]

        list_users_response = await test_client.get("/api/auth/users", headers=admin_headers)
        assert list_users_response.status_code == 200, list_users_response.text
        users_before_delete = list_users_response.json()
        department_admin = next(
            (user for user in users_before_delete if user["uid"] == department_payload["admin_uid"]),
            None,
        )
        assert department_admin is not None
        assert "role" not in department_admin
        assert [role["code"] for role in department_admin["roles"]] == ["admin"]
        department_admin_id = department_admin["id"]

        delete_department_response = await test_client.delete(
            f"/api/departments/{department_id}", headers=admin_headers
        )
        assert delete_department_response.status_code == 400, delete_department_response.text
        assert "直属用户" in delete_department_response.json()["detail"]

        existing_department_response = await test_client.get(
            f"/api/departments/{department_id}",
            headers=admin_headers,
        )
        assert existing_department_response.status_code == 200, existing_department_response.text

        list_users_after_rejection = await test_client.get("/api/auth/users", headers=admin_headers)
        assert list_users_after_rejection.status_code == 200, list_users_after_rejection.text
        users_after_rejection = list_users_after_rejection.json()

        existing_admin = next((user for user in users_after_rejection if user["id"] == department_admin_id), None)
        assert existing_admin is not None
        assert existing_admin["department_id"] == department_id

        existing_user = next((user for user in users_after_rejection if user["id"] == created_user_id), None)
        assert existing_user is not None
        assert existing_user["department_id"] == department_id
    finally:
        if department_admin_id is not None:
            await test_client.delete(f"/api/auth/users/{department_admin_id}", headers=admin_headers)
        if created_user_id is not None:
            await test_client.delete(f"/api/auth/users/{created_user_id}", headers=admin_headers)
        if department_id is not None:
            cleanup_response = await test_client.delete(f"/api/departments/{department_id}", headers=admin_headers)
            assert cleanup_response.status_code in (200, 404), cleanup_response.text


async def test_superadmin_cannot_delete_group_root(test_client, admin_headers):
    departments_response = await test_client.get("/api/departments", headers=admin_headers)
    assert departments_response.status_code == 200, departments_response.text
    departments = departments_response.json()

    # 集团根在代码中固定为 id=1（受保护不可删除），其名称可被用户修改，故按 id 定位
    group_root = next((department for department in departments if department["id"] == ROOT_DEPARTMENT_ID), None)
    assert group_root is not None
    assert group_root["parent_id"] is None

    delete_response = await test_client.delete(f"/api/departments/{group_root['id']}", headers=admin_headers)
    assert delete_response.status_code == 400, delete_response.text
    assert delete_response.json()["detail"] == "集团根不允许删除"
