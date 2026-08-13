"""
Integration tests for department management API routes.
"""

from __future__ import annotations

import uuid

import pytest

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


async def _delete_nodes(test_client, admin_headers, node_ids):
    """按传入顺序删除组织节点，用于用例清理（调用方需先删子后删父）。"""
    for node_id in node_ids:
        if node_id is not None:
            await test_client.delete(f"/api/departments/{node_id}", headers=admin_headers)


async def test_superadmin_can_build_multi_level_tree(test_client, admin_headers):
    suffix = uuid.uuid4().hex[:8]
    company_id = None
    department_id = None

    try:
        company_response = await _create_node(
            test_client, admin_headers, f"pytest_company_{suffix}", node_type="company"
        )
        assert company_response.status_code == 201, company_response.text
        company = company_response.json()
        company_id = company["id"]
        assert company["parent_id"] == ROOT_DEPARTMENT_ID
        assert company["node_type"] == "company"

        department_response = await _create_node(
            test_client, admin_headers, f"pytest_dept_{suffix}", parent_id=company_id
        )
        assert department_response.status_code == 201, department_response.text
        department = department_response.json()
        department_id = department["id"]
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
    finally:
        await _delete_nodes(test_client, admin_headers, [department_id, company_id])


async def test_superadmin_can_move_subtree(test_client, admin_headers):
    """移动组织节点时，其整棵子树仍按新祖先链连续展示。"""
    suffix = uuid.uuid4().hex[:8]
    first_company_id = None
    second_company_id = None
    division_id = None
    department_id = None

    try:
        first_company_id = await _create_node_id(
            test_client, admin_headers, f"pytest_move_a_{suffix}", node_type="company"
        )
        second_company_id = await _create_node_id(
            test_client, admin_headers, f"pytest_move_b_{suffix}", node_type="company"
        )
        division_id = await _create_node_id(
            test_client, admin_headers, f"pytest_division_{suffix}", parent_id=first_company_id
        )
        department_id = await _create_node_id(
            test_client, admin_headers, f"pytest_team_{suffix}", parent_id=division_id
        )

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
    finally:
        await _delete_nodes(
            test_client,
            admin_headers,
            [department_id, division_id, first_company_id, second_company_id],
        )


async def test_move_rejects_self_and_descendant_parent(test_client, admin_headers):
    """组织节点不能移动到自身或自身后代之下。"""
    suffix = uuid.uuid4().hex[:8]
    parent_id = None
    child_id = None

    try:
        parent_id = await _create_node_id(test_client, admin_headers, f"pytest_cycle_parent_{suffix}")
        child_id = await _create_node_id(
            test_client, admin_headers, f"pytest_cycle_child_{suffix}", parent_id=parent_id
        )

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
    finally:
        await _delete_nodes(test_client, admin_headers, [child_id, parent_id])


async def test_move_rejects_empty_and_missing_parent(test_client, admin_headers):
    """移动请求必须指定一个实际存在的父节点。"""
    suffix = uuid.uuid4().hex[:8]
    node_id = None

    try:
        node_id = await _create_node_id(test_client, admin_headers, f"pytest_move_target_{suffix}")

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
    finally:
        await _delete_nodes(test_client, admin_headers, [node_id])


async def test_move_rejects_group_root(test_client, admin_headers):
    """集团根不可移动。"""
    suffix = uuid.uuid4().hex[:8]
    target_id = None

    try:
        target_id = await _create_node_id(test_client, admin_headers, f"pytest_root_target_{suffix}")

        response = await test_client.put(
            f"/api/departments/{ROOT_DEPARTMENT_ID}", json={"parent_id": target_id}, headers=admin_headers
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "集团根不允许移动"
    finally:
        await _delete_nodes(test_client, admin_headers, [target_id])


async def test_move_rejects_duplicate_name_under_target_parent(test_client, admin_headers):
    """移动不会在目标父节点下制造同名组织节点。"""
    suffix = uuid.uuid4().hex[:8]
    shared_name = f"pytest_move_dup_{suffix}"
    first_company_id = None
    second_company_id = None
    source_id = None
    existing_id = None

    try:
        first_company_id = await _create_node_id(test_client, admin_headers, f"pytest_dup_a_{suffix}")
        second_company_id = await _create_node_id(test_client, admin_headers, f"pytest_dup_b_{suffix}")
        source_id = await _create_node_id(test_client, admin_headers, shared_name, parent_id=first_company_id)
        existing_id = await _create_node_id(test_client, admin_headers, shared_name, parent_id=second_company_id)

        response = await test_client.put(
            f"/api/departments/{source_id}", json={"parent_id": second_company_id}, headers=admin_headers
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "同一父级下已存在同名组织节点"
    finally:
        await _delete_nodes(
            test_client,
            admin_headers,
            [source_id, existing_id, first_company_id, second_company_id],
        )


async def test_same_name_is_allowed_under_different_parents(test_client, admin_headers):
    suffix = uuid.uuid4().hex[:8]
    shared_name = f"pytest_hr_{suffix}"
    first_company_id = None
    second_company_id = None
    first_hr_id = None
    second_hr_id = None

    try:
        first_company_id = await _create_node_id(
            test_client, admin_headers, f"pytest_co_a_{suffix}", node_type="company"
        )
        second_company_id = await _create_node_id(
            test_client, admin_headers, f"pytest_co_b_{suffix}", node_type="company"
        )

        first_hr_response = await _create_node(test_client, admin_headers, shared_name, parent_id=first_company_id)
        assert first_hr_response.status_code == 201, first_hr_response.text
        first_hr_id = first_hr_response.json()["id"]

        second_hr_response = await _create_node(test_client, admin_headers, shared_name, parent_id=second_company_id)
        assert second_hr_response.status_code == 201, second_hr_response.text
        second_hr_id = second_hr_response.json()["id"]

        assert first_hr_id != second_hr_id
    finally:
        await _delete_nodes(
            test_client,
            admin_headers,
            [first_hr_id, second_hr_id, first_company_id, second_company_id],
        )


async def test_duplicate_name_under_same_parent_is_rejected(test_client, admin_headers):
    suffix = uuid.uuid4().hex[:8]
    shared_name = f"pytest_dup_{suffix}"
    company_id = None
    first_child_id = None

    try:
        company_id = await _create_node_id(test_client, admin_headers, f"pytest_co_{suffix}", node_type="company")

        first_child_response = await _create_node(test_client, admin_headers, shared_name, parent_id=company_id)
        assert first_child_response.status_code == 201, first_child_response.text
        first_child_id = first_child_response.json()["id"]

        duplicate_response = await _create_node(test_client, admin_headers, shared_name, parent_id=company_id)
        assert duplicate_response.status_code == 400, duplicate_response.text
        assert duplicate_response.json()["detail"] == "同一父级下已存在同名组织节点"
    finally:
        await _delete_nodes(test_client, admin_headers, [first_child_id, company_id])


async def test_create_department_without_admin_account(test_client, admin_headers):
    suffix = uuid.uuid4().hex[:8]
    department_id = None

    try:
        response = await _create_node(test_client, admin_headers, f"pytest_no_admin_{suffix}")
        assert response.status_code == 201, response.text
        body = response.json()
        department_id = body["id"]
        assert body["user_count"] == 0
    finally:
        await _delete_nodes(test_client, admin_headers, [department_id])


async def test_delete_department_with_children_is_rejected(test_client, admin_headers):
    suffix = uuid.uuid4().hex[:8]
    parent_id = None
    child_id = None

    try:
        parent_id = await _create_node_id(test_client, admin_headers, f"pytest_parent_{suffix}", node_type="company")
        child_id = await _create_node_id(test_client, admin_headers, f"pytest_child_{suffix}", parent_id=parent_id)

        delete_response = await test_client.delete(f"/api/departments/{parent_id}", headers=admin_headers)
        assert delete_response.status_code == 400, delete_response.text
        assert "子节点" in delete_response.json()["detail"]

        still_exists = await test_client.get(f"/api/departments/{parent_id}", headers=admin_headers)
        assert still_exists.status_code == 200, still_exists.text
    finally:
        await _delete_nodes(test_client, admin_headers, [child_id, parent_id])


async def test_superadmin_can_delete_department_with_users(test_client, admin_headers):
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
        "role": "user",
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
        department_admin_id = department_admin["id"]

        delete_department_response = await test_client.delete(
            f"/api/departments/{department_id}", headers=admin_headers
        )
        assert delete_department_response.status_code == 200, delete_department_response.text
        assert delete_department_response.json()["success"] is True
        department_id = None

        deleted_department_response = await test_client.get(
            f"/api/departments/{create_department_response.json()['id']}",
            headers=admin_headers,
        )
        assert deleted_department_response.status_code == 404, deleted_department_response.text

        list_users_after_delete_response = await test_client.get("/api/auth/users", headers=admin_headers)
        assert list_users_after_delete_response.status_code == 200, list_users_after_delete_response.text
        users_after_delete = list_users_after_delete_response.json()

        # 删除组织节点后用户回落集团根（代码中集团根固定为 id=1，其名称可被用户修改）
        migrated_admin = next((user for user in users_after_delete if user["id"] == department_admin_id), None)
        assert migrated_admin is not None
        assert migrated_admin["department_id"] == ROOT_DEPARTMENT_ID

        migrated_user = next((user for user in users_after_delete if user["id"] == created_user_id), None)
        assert migrated_user is not None
        assert migrated_user["department_id"] == ROOT_DEPARTMENT_ID
    finally:
        if department_admin_id is not None:
            await test_client.delete(f"/api/auth/users/{department_admin_id}", headers=admin_headers)
        if created_user_id is not None:
            await test_client.delete(f"/api/auth/users/{created_user_id}", headers=admin_headers)
        if department_id is not None:
            await test_client.delete(f"/api/departments/{department_id}", headers=admin_headers)


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
