"""
Integration tests for knowledge router endpoints.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from test.integration.api.helpers import create_api_user, create_department, delete_department_by_id, delete_user_by_id
from test.support.knowledge import default_embedding_model_spec
from yuxi.modules.knowledge.chunking.presets import CHUNK_PRESET_IDS

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _assert_forbidden_response(response):
    """验证 403 禁止访问响应的格式"""
    assert response.status_code == 403
    payload = response.json()
    assert "detail" in payload
    assert isinstance(payload["detail"], str)


def _assert_not_found_response(response):
    """验证越出知识库共享范围时不暴露资源存在性。"""

    assert response.status_code == 404
    assert isinstance(response.json().get("detail"), str)


async def _create_test_department(test_client, admin_headers, prefix="pytest_dept", parent_id=None):
    suffix = uuid.uuid4().hex[:8]
    admin_uid = f"deptadmin_{suffix}"
    payload = {
        "name": f"{prefix}_{suffix}",
        "description": "pytest department",
        "admin_uid": admin_uid,
        "admin_password": f"Pw!{suffix}",
    }
    if parent_id is not None:
        payload["parent_id"] = parent_id
    payload = await create_department(test_client, admin_headers, payload)
    payload["admin_uid"] = admin_uid
    return payload


async def _create_test_user(test_client, admin_headers, department_id):
    suffix = uuid.uuid4().hex[:8]
    password = f"Pw!{suffix}"
    user = await create_api_user(
        test_client,
        admin_headers,
        {
            "username": f"pytest_user_{suffix}",
            "password": password,
            "department_id": department_id,
        },
    )

    login_response = await test_client.post(
        "/api/auth/token",
        data={"username": user["uid"], "password": password},
    )
    assert login_response.status_code == 200, login_response.text
    return {"user": user, "headers": {"Authorization": f"Bearer {login_response.json()['access_token']}"}}


async def _find_user_id_by_uid(test_client, admin_headers, uid):
    response = await test_client.get("/api/auth/users", headers=admin_headers)
    assert response.status_code == 200, response.text
    for user in response.json():
        if user["uid"] == uid:
            return user["id"]
    return None


async def _delete_department_with_admin(test_client, admin_headers, department):
    admin_user_id = await _find_user_id_by_uid(test_client, admin_headers, department["admin_uid"])
    if admin_user_id:
        await delete_user_by_id(test_client, admin_headers, admin_user_id)
    await delete_department_by_id(test_client, admin_headers, department["id"])


async def _create_test_knowledge_base(test_client, admin_headers, share_config=None):
    response = await test_client.post(
        "/api/knowledge/knowledge-bases",
        json={
            "name": f"pytest_acl_{uuid.uuid4().hex[:8]}",
            "description": "Knowledge permission test",
            "embedding_model_spec": default_embedding_model_spec(),
            "kb_type": "milvus",
            "additional_params": {},
            "share_config": share_config,
        },
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _department_share_config(department_id):
    """构造组织节点子树可读、管理员可管的共享配置。"""
    scope = {"access_level": "department", "department_ids": [department_id], "user_uids": []}
    return {"version": 2, "read_scope": scope, "manage_scope": scope}


async def _accessible_kb_ids(test_client, headers):
    response = await test_client.get("/api/knowledge/knowledge-bases/accessible", headers=headers)
    assert response.status_code == 200, response.text
    return {item["kb_id"] for item in response.json().get("knowledge_bases", [])}


async def test_admin_can_manage_knowledge_bases(test_client, admin_headers, knowledge_base_resource):
    kb_id = knowledge_base_resource["kb_id"]

    list_response = await test_client.get("/api/knowledge/knowledge-bases", headers=admin_headers)
    assert list_response.status_code == 200, list_response.text
    knowledge_bases = list_response.json().get("knowledge_bases", [])
    knowledge_base = next(entry for entry in knowledge_bases if entry["kb_id"] == kb_id)
    assert knowledge_base["metadata"] == knowledge_base["additional_params"]
    assert knowledge_base["status"] == "已连接"
    assert knowledge_base["row_count"] == (knowledge_base["stats"]["row_count"] or knowledge_base["stats"]["file_count"])
    assert knowledge_base["effective_permission"] == "manage"
    assert knowledge_base["can_manage"] is True

    get_response = await test_client.get(f"/api/knowledge/knowledge-bases/{kb_id}", headers=admin_headers)
    assert get_response.status_code == 200, get_response.text
    detail = get_response.json()
    assert detail["kb_id"] == kb_id
    assert detail["metadata"] == detail["additional_params"]
    assert detail["stats"]["row_count"] == detail["row_count"]

    update_response = await test_client.put(
        f"/api/knowledge/knowledge-bases/{kb_id}",
        json={"name": knowledge_base_resource["name"], "description": "Updated by pytest"},
        headers=admin_headers,
    )
    assert update_response.status_code == 200, update_response.text
    assert update_response.json()["knowledge_base"]["description"] == "Updated by pytest"
    assert "database" not in update_response.json()
    assert "databases" not in list_response.json()

    engine = create_async_engine(os.environ["POSTGRES_URL"])
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(text("SELECT name, description FROM knowledge_bases WHERE kb_id = :kb_id"), {"kb_id": kb_id})
            ).one()
            assert row.name == knowledge_base_resource["name"]
            assert row.description == "Updated by pytest"
    finally:
        await engine.dispose()


async def test_knowledge_base_creation_requires_name(test_client, admin_headers):
    """仅包含旧名称字段的创建请求因缺少新字段而失败。"""
    response = await test_client.post(
        "/api/knowledge/knowledge-bases",
        json={"database_name": "pytest_old_field", "description": "Missing new name"},
        headers=admin_headers,
    )
    assert response.status_code == 422, response.text
    assert any(error["loc"] == ["body", "name"] and error["type"] == "missing" for error in response.json()["detail"])


async def test_document_exists_returns_false_for_missing_relative_path(test_client, admin_headers, knowledge_base_resource):
    kb_id = knowledge_base_resource["kb_id"]
    filename = f"google_drive/shared_drives/engineering/serving-runtime/dsid_{uuid.uuid4().hex}__missing-playbook.txt"

    response = await test_client.get(
        f"/api/knowledge/knowledge-bases/{kb_id}/documents/exists",
        params={"filename": filename},
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"kb_id": kb_id, "filename": filename, "exists": False}


async def test_folder_rename_and_move_persist_tree_changes(test_client, admin_headers, knowledge_base_resource):
    kb_id = knowledge_base_resource["kb_id"]

    async def create_folder(name, parent_id=None):
        response = await test_client.post(
            f"/api/knowledge/knowledge-bases/{kb_id}/folders",
            json={"folder_name": name, "parent_id": parent_id},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        return response.json()

    source = await create_folder(f"source-{uuid.uuid4().hex[:6]}")
    child = await create_folder("child", source["file_id"])
    destination = await create_folder(f"destination-{uuid.uuid4().hex[:6]}")
    assert source["created_at"]
    assert source["created_by"]

    rename_response = await test_client.put(
        f"/api/knowledge/knowledge-bases/{kb_id}/folders/{source['file_id']}/rename",
        json={"folder_name": "renamed source"},
        headers=admin_headers,
    )
    assert rename_response.status_code == 200, rename_response.text
    assert rename_response.json()["filename"] == "renamed source"
    assert rename_response.json()["path"] == "renamed source"

    source_listing = await test_client.get(
        f"/api/knowledge/knowledge-bases/{kb_id}/documents",
        params={"parent_id": source["file_id"]},
        headers=admin_headers,
    )
    assert source_listing.status_code == 200, source_listing.text
    assert [(item["file_id"], item["parent_id"], item["filename"], item["created_by"]) for item in source_listing.json()["items"]] == [
        (child["file_id"], source["file_id"], "child", child["created_by"])
    ]

    move_response = await test_client.put(
        f"/api/knowledge/knowledge-bases/{kb_id}/documents/{child['file_id']}/move",
        json={"new_parent_id": destination["file_id"]},
        headers=admin_headers,
    )
    assert move_response.status_code == 200, move_response.text
    assert move_response.json()["parent_id"] == destination["file_id"]

    destination_listing = await test_client.get(
        f"/api/knowledge/knowledge-bases/{kb_id}/documents",
        params={"parent_id": destination["file_id"]},
        headers=admin_headers,
    )
    assert destination_listing.status_code == 200, destination_listing.text
    assert [item["file_id"] for item in destination_listing.json()["items"]] == [child["file_id"]]

    move_to_root_response = await test_client.put(
        f"/api/knowledge/knowledge-bases/{kb_id}/documents/{child['file_id']}/move",
        json={"new_parent_id": None},
        headers=admin_headers,
    )
    assert move_to_root_response.status_code == 200, move_to_root_response.text
    assert move_to_root_response.json()["parent_id"] is None

    root_listing = await test_client.get(
        f"/api/knowledge/knowledge-bases/{kb_id}/documents",
        headers=admin_headers,
    )
    assert root_listing.status_code == 200, root_listing.text
    assert child["file_id"] in {item["file_id"] for item in root_listing.json()["items"]}

    missing_target_response = await test_client.put(
        f"/api/knowledge/knowledge-bases/{kb_id}/documents/{child['file_id']}/move",
        json={},
        headers=admin_headers,
    )
    assert missing_target_response.status_code == 422, missing_target_response.text


async def test_folder_mutations_reject_invalid_name_and_directory_cycle(test_client, admin_headers, knowledge_base_resource):
    kb_id = knowledge_base_resource["kb_id"]

    parent_response = await test_client.post(
        f"/api/knowledge/knowledge-bases/{kb_id}/folders",
        json={"folder_name": f"parent-{uuid.uuid4().hex[:6]}", "parent_id": None},
        headers=admin_headers,
    )
    assert parent_response.status_code == 200, parent_response.text
    parent = parent_response.json()

    child_response = await test_client.post(
        f"/api/knowledge/knowledge-bases/{kb_id}/folders",
        json={"folder_name": "child", "parent_id": parent["file_id"]},
        headers=admin_headers,
    )
    assert child_response.status_code == 200, child_response.text
    child = child_response.json()

    invalid_rename = await test_client.put(
        f"/api/knowledge/knowledge-bases/{kb_id}/folders/{parent['file_id']}/rename",
        json={"folder_name": "invalid/name"},
        headers=admin_headers,
    )
    assert invalid_rename.status_code == 422, invalid_rename.text
    assert "分隔符" in invalid_rename.json()["detail"]

    cycle_move = await test_client.put(
        f"/api/knowledge/knowledge-bases/{kb_id}/documents/{parent['file_id']}/move",
        json={"new_parent_id": child["file_id"]},
        headers=admin_headers,
    )
    assert cycle_move.status_code == 400, cycle_move.text
    assert "own subfolder" in cycle_move.json()["detail"]


async def test_concurrent_folder_moves_cannot_create_cycle(test_client, admin_headers, knowledge_base_resource):
    kb_id = knowledge_base_resource["kb_id"]

    async def create_folder(name):
        response = await test_client.post(
            f"/api/knowledge/knowledge-bases/{kb_id}/folders",
            json={"folder_name": name, "parent_id": None},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        return response.json()

    folder_a = await create_folder(f"concurrent-a-{uuid.uuid4().hex[:6]}")
    folder_b = await create_folder(f"concurrent-b-{uuid.uuid4().hex[:6]}")
    responses = await asyncio.gather(
        test_client.put(
            f"/api/knowledge/knowledge-bases/{kb_id}/documents/{folder_a['file_id']}/move",
            json={"new_parent_id": folder_b["file_id"]},
            headers=admin_headers,
        ),
        test_client.put(
            f"/api/knowledge/knowledge-bases/{kb_id}/documents/{folder_b['file_id']}/move",
            json={"new_parent_id": folder_a["file_id"]},
            headers=admin_headers,
        ),
    )
    assert sorted(response.status_code for response in responses) == [200, 400]

    root_listing = await test_client.get(
        f"/api/knowledge/knowledge-bases/{kb_id}/documents",
        headers=admin_headers,
    )
    assert root_listing.status_code == 200, root_listing.text
    folder_ids = {folder_a["file_id"], folder_b["file_id"]}
    root_ids = {item["file_id"] for item in root_listing.json()["items"]} & folder_ids
    assert len(root_ids) == 1

    root_id = root_ids.pop()
    child_listing = await test_client.get(
        f"/api/knowledge/knowledge-bases/{kb_id}/documents",
        params={"parent_id": root_id},
        headers=admin_headers,
    )
    assert child_listing.status_code == 200, child_listing.text
    assert {item["file_id"] for item in child_listing.json()["items"]} & folder_ids == folder_ids - {root_id}


async def test_folder_move_waits_for_kb_tree_lock(test_client, admin_headers, knowledge_base_resource):
    kb_id = knowledge_base_resource["kb_id"]

    async def create_folder(name):
        response = await test_client.post(
            f"/api/knowledge/knowledge-bases/{kb_id}/folders",
            json={"folder_name": name, "parent_id": None},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        return response.json()

    source = await create_folder(f"locked-source-{uuid.uuid4().hex[:6]}")
    destination = await create_folder(f"locked-destination-{uuid.uuid4().hex[:6]}")
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    move_task = None
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:kb_id))"),
                {"kb_id": kb_id},
            )
            move_task = asyncio.create_task(
                test_client.put(
                    f"/api/knowledge/knowledge-bases/{kb_id}/documents/{source['file_id']}/move",
                    json={"new_parent_id": destination["file_id"]},
                    headers=admin_headers,
                )
            )
            await asyncio.sleep(0.1)
            assert not move_task.done(), "移动请求未等待知识库目录树锁"

        move_response = await asyncio.wait_for(move_task, timeout=2)
        assert move_response.status_code == 200, move_response.text
        assert move_response.json()["parent_id"] == destination["file_id"]
    finally:
        if move_task is not None and not move_task.done():
            move_task.cancel()
            await asyncio.gather(move_task, return_exceptions=True)
        await engine.dispose()


async def test_create_knowledge_base_with_chunk_preset(test_client, admin_headers):
    db_name = f"pytest_chunk_preset_{uuid.uuid4().hex[:6]}"
    payload = {
        "name": db_name,
        "description": "Chunk preset create test",
        "embedding_model_spec": default_embedding_model_spec(),
        "kb_type": "milvus",
        "additional_params": {"chunk_preset_id": "book"},
    }

    create_response = await test_client.post("/api/knowledge/knowledge-bases", json=payload, headers=admin_headers)
    assert create_response.status_code == 200, create_response.text
    create_payload = create_response.json()
    assert create_payload["files"] == {}
    kb_id = create_payload["kb_id"]

    info_response = await test_client.get(f"/api/knowledge/knowledge-bases/{kb_id}", headers=admin_headers)
    assert info_response.status_code == 200, info_response.text
    assert info_response.json()["additional_params"]["chunk_preset_id"] == "book"

    delete_response = await test_client.delete(f"/api/knowledge/knowledge-bases/{kb_id}", headers=admin_headers)
    assert delete_response.status_code == 200, delete_response.text


async def test_get_chunk_presets_returns_configured_options(test_client, admin_headers):
    response = await test_client.get("/api/knowledge/chunk-presets", headers=admin_headers)
    assert response.status_code == 200, response.text

    payload = response.json()
    options = payload["chunk_presets"]
    assert payload["message"] == "success"
    assert {option["value"] for option in options} == CHUNK_PRESET_IDS
    assert all(set(option) == {"value", "label", "description"} for option in options)
    assert all(option["label"] and option["description"] for option in options)


async def test_update_knowledge_base_additional_params_merge_keeps_chunk_preset(test_client, admin_headers, knowledge_base_resource):
    kb_id = knowledge_base_resource["kb_id"]

    first_update = await test_client.put(
        f"/api/knowledge/knowledge-bases/{kb_id}",
        json={
            "name": knowledge_base_resource["name"],
            "description": "update with chunk preset",
            "additional_params": {"chunk_preset_id": "qa"},
        },
        headers=admin_headers,
    )
    assert first_update.status_code == 200, first_update.text

    second_update = await test_client.put(
        f"/api/knowledge/knowledge-bases/{kb_id}",
        json={
            "name": knowledge_base_resource["name"],
            "description": "update without additional params",
        },
        headers=admin_headers,
    )
    assert second_update.status_code == 200, second_update.text

    info_response = await test_client.get(f"/api/knowledge/knowledge-bases/{kb_id}", headers=admin_headers)
    assert info_response.status_code == 200, info_response.text
    assert info_response.json()["additional_params"]["chunk_preset_id"] == "qa"


async def test_knowledge_routes_follow_read_and_manage_permissions(test_client, standard_user, knowledge_base_resource):
    kb_id = knowledge_base_resource["kb_id"]

    forbidden_create = await test_client.post(
        "/api/knowledge/knowledge-bases",
        json={
            "name": f"unauthorized_{uuid.uuid4().hex[:8]}",
            "description": "Should not succeed",
            "embedding_model_spec": "siliconflow-cn:Pro/BAAI/bge-m3",
        },
        headers=standard_user["headers"],
    )
    _assert_forbidden_response(forbidden_create)

    list_response = await test_client.get("/api/knowledge/knowledge-bases", headers=standard_user["headers"])
    assert list_response.status_code == 200, list_response.text
    assert kb_id in {item["kb_id"] for item in list_response.json()["knowledge_bases"]}

    chunk_presets = await test_client.get("/api/knowledge/chunk-presets", headers=standard_user["headers"])
    assert chunk_presets.status_code == 200, chunk_presets.text

    get_response = await test_client.get(f"/api/knowledge/knowledge-bases/{kb_id}", headers=standard_user["headers"])
    assert get_response.status_code == 200, get_response.text

    exists_response = await test_client.get(
        f"/api/knowledge/knowledge-bases/{kb_id}/documents/exists",
        params={"filename": "demo.txt"},
        headers=standard_user["headers"],
    )
    assert exists_response.status_code == 200, exists_response.text

    forbidden_update = await test_client.put(
        f"/api/knowledge/knowledge-bases/{kb_id}",
        json={"name": knowledge_base_resource["name"], "description": "Should not succeed"},
        headers=standard_user["headers"],
    )
    _assert_forbidden_response(forbidden_update)


async def test_kb_image_proxy_requires_auth_and_streams_private_image(test_client, admin_headers, knowledge_base_resource):
    """知识库图片代理：未登录不可访问，鉴权后可读取私有 bucket 图片"""
    from yuxi.infrastructure.minio.client import MinIOClient, get_minio_client

    kb_id = knowledge_base_resource["kb_id"]
    image_name = f"proxy_{uuid.uuid4().hex[:8]}.png"
    object_name = f"{kb_id}/kb-images/{image_name}"
    image_bytes = b"\x89PNG\r\n\x1a\nfake-image-content"

    minio_client = get_minio_client()
    minio_client.upload_file(
        bucket_name=MinIOClient.KB_BUCKETS["images"],
        object_name=object_name,
        data=image_bytes,
        content_type="image/png",
    )

    proxy_path = f"/api/knowledge/knowledge-bases/{kb_id}/images/kb-images/{image_name}"

    anonymous = await test_client.get(proxy_path)
    assert anonymous.status_code == 401

    invalid_headers = {"Authorization": "Bearer invalid-token"}
    forbidden = await test_client.get(proxy_path, headers=invalid_headers)
    assert forbidden.status_code == 401

    authorized = await test_client.get(proxy_path, headers=admin_headers)
    assert authorized.status_code == 200, authorized.text
    assert authorized.content == image_bytes
    assert authorized.headers["content-type"].startswith("image/png")


async def test_kb_image_proxy_rejects_invalid_or_missing_object(test_client, admin_headers, knowledge_base_resource):
    """知识库图片代理：非法路径与不存在的图片返回 400/404"""
    kb_id = knowledge_base_resource["kb_id"]

    invalid_path = await test_client.get(
        f"/api/knowledge/knowledge-bases/{kb_id}/images/avatar/user.png",
        headers=admin_headers,
    )
    assert invalid_path.status_code == 400

    traversal_path = await test_client.get(
        f"/api/knowledge/knowledge-bases/{kb_id}/images/kb-images/..%2Fother.png",
        headers=admin_headers,
    )
    assert traversal_path.status_code == 400

    backslash_path = await test_client.get(
        f"/api/knowledge/knowledge-bases/{kb_id}/images/kb-images/..%5Cother.png",
        headers=admin_headers,
    )
    assert backslash_path.status_code == 400

    missing_image = await test_client.get(
        f"/api/knowledge/knowledge-bases/{kb_id}/images/kb-images/missing.png",
        headers=admin_headers,
    )
    assert missing_image.status_code == 404


async def test_admin_can_create_vector_db_with_reranker(test_client, admin_headers):
    """测试创建向量库并配置 reranker 参数（通过 query_params.options）

    注意：数据库清理由 conftest.py 中的 session fixture 自动处理。
    """
    db_name = f"pytest_rerank_{uuid.uuid4().hex[:6]}"
    payload = {
        "name": db_name,
        "description": "Vector DB with reranker",
        "embedding_model_spec": default_embedding_model_spec(),
        "kb_type": "milvus",
        "additional_params": {},
    }

    create_response = await test_client.post("/api/knowledge/knowledge-bases", json=payload, headers=admin_headers)
    assert create_response.status_code == 200, create_response.text

    db_payload = create_response.json()
    kb_id = db_payload["kb_id"]

    # 获取查询参数配置
    params_response = await test_client.get(f"/api/knowledge/knowledge-bases/{kb_id}/query-params", headers=admin_headers)
    assert params_response.status_code == 200, params_response.text

    params_payload = params_response.json()
    options = params_payload.get("params", {}).get("options", [])
    option_keys = {option.get("key") for option in options}

    # 验证新的参数名称
    assert "final_top_k" in option_keys
    assert "use_reranker" in option_keys
    assert "recall_top_k" in option_keys
    assert "reranker_model" in option_keys

    # 验证参数配置
    final_top_k_option = next((opt for opt in options if opt.get("key") == "final_top_k"), None)
    assert final_top_k_option is not None
    assert final_top_k_option.get("default") == 10

    use_reranker_option = next((opt for opt in options if opt.get("key") == "use_reranker"), None)
    assert use_reranker_option is not None
    assert use_reranker_option.get("default") is False

    # 保存查询参数（模拟前端配置）
    update_params = {
        "final_top_k": 5,
        "use_reranker": True,
        "recall_top_k": 20,
    }
    update_response = await test_client.put(
        f"/api/knowledge/knowledge-bases/{kb_id}/query-params",
        json=update_params,
        headers=admin_headers,
    )
    assert update_response.status_code == 200, update_response.text

    # 再次获取参数，验证保存成功
    params_response2 = await test_client.get(f"/api/knowledge/knowledge-bases/{kb_id}/query-params", headers=admin_headers)
    assert params_response2.status_code == 200, params_response2.text

    params_payload2 = params_response2.json()
    options2 = params_payload2.get("params", {}).get("options", [])

    # 验证保存的值
    final_top_k_option2 = next((opt for opt in options2 if opt.get("key") == "final_top_k"), None)
    assert final_top_k_option2 is not None
    assert final_top_k_option2.get("default") == 5  # 保存的值

    use_reranker_option2 = next((opt for opt in options2 if opt.get("key") == "use_reranker"), None)
    assert use_reranker_option2 is not None
    assert use_reranker_option2.get("default") is True  # 保存的值


async def test_concurrent_query_param_updates_preserve_all_options(test_client, admin_headers):
    """并发的部分更新应在数据库事务内合并，而不是后写覆盖先写。"""
    payload = {
        "name": f"pytest_query_params_{uuid.uuid4().hex[:6]}",
        "description": "Concurrent query params update",
        "kb_type": "dify",
        "additional_params": {
            "dify_api_url": "https://api.dify.ai/v1",
            "dify_token": "test-token",
            "dify_dataset_id": "dataset-123",
        },
    }
    create_response = await test_client.post("/api/knowledge/knowledge-bases", json=payload, headers=admin_headers)
    assert create_response.status_code == 200, create_response.text
    kb_id = create_response.json()["kb_id"]
    endpoint = f"/api/knowledge/knowledge-bases/{kb_id}/query-params"

    first_response, second_response = await asyncio.gather(
        test_client.put(endpoint, json={"final_top_k": 7}, headers=admin_headers),
        test_client.put(endpoint, json={"similarity_threshold": 0.42}, headers=admin_headers),
    )

    assert first_response.status_code == 200, first_response.text
    assert second_response.status_code == 200, second_response.text

    params_response = await test_client.get(endpoint, headers=admin_headers)
    assert params_response.status_code == 200, params_response.text
    options = params_response.json()["params"]["options"]
    saved_options = {option["key"]: option["default"] for option in options}
    assert saved_options["final_top_k"] == 7
    assert saved_options["similarity_threshold"] == 0.42


@pytest.mark.parametrize(
    ("case", "additional_params", "expected_status", "expected_detail"),
    [
        (
            "valid",
            {"dify_api_url": "https://api.dify.ai/v1", "dify_token": "test-token", "dify_dataset_id": "dataset-123"},
            200,
            None,
        ),
        (
            "missing-credentials",
            {"dify_api_url": "https://api.dify.ai/v1", "dify_token": "", "dify_dataset_id": ""},
            400,
            "Dify 参数缺失",
        ),
        (
            "invalid-api-url",
            {"dify_api_url": "https://api.dify.ai", "dify_token": "test-token", "dify_dataset_id": "dataset-123"},
            400,
            "/v1",
        ),
    ],
    ids=["valid", "missing-credentials", "invalid-api-url"],
)
async def test_create_dify_knowledge_base_validates_and_persists(
    test_client, admin_headers, case, additional_params, expected_status, expected_detail
):
    payload = {
        "name": f"pytest_dify_{case}_{uuid.uuid4().hex[:6]}",
        "description": f"Dify KB {case}",
        "kb_type": "dify",
        "additional_params": additional_params,
    }
    response = await test_client.post("/api/knowledge/knowledge-bases", json=payload, headers=admin_headers)
    assert response.status_code == expected_status, response.text
    if expected_status != 200:
        assert expected_detail in response.json()["detail"]
        return

    created_payload = response.json()
    kb_id = created_payload["kb_id"]
    assert created_payload["embedding_model_spec"] is None
    assert "chunk_preset_id" not in created_payload["metadata"]
    info_response = await test_client.get(f"/api/knowledge/knowledge-bases/{kb_id}", headers=admin_headers)
    assert info_response.status_code == 200, info_response.text
    persisted_params = info_response.json()["additional_params"]
    assert persisted_params["dify_api_url"] == additional_params["dify_api_url"]
    assert persisted_params["dify_token"] == additional_params["dify_token"]
    assert persisted_params["dify_dataset_id"] == additional_params["dify_dataset_id"]


async def test_dify_query_params_and_documents_readonly(test_client, admin_headers):
    payload = {
        "name": f"pytest_dify_ro_{uuid.uuid4().hex[:6]}",
        "description": "Dify readonly routes",
        "kb_type": "dify",
        "additional_params": {
            "dify_api_url": "https://api.dify.ai/v1",
            "dify_token": "test-token",
            "dify_dataset_id": "dataset-123",
        },
    }

    create_response = await test_client.post("/api/knowledge/knowledge-bases", json=payload, headers=admin_headers)
    assert create_response.status_code == 200, create_response.text
    kb_id = create_response.json()["kb_id"]

    params_response = await test_client.get(f"/api/knowledge/knowledge-bases/{kb_id}/query-params", headers=admin_headers)
    assert params_response.status_code == 200, params_response.text
    options = params_response.json().get("params", {}).get("options", [])
    option_keys = {item.get("key") for item in options}
    assert option_keys == {"search_mode", "final_top_k", "score_threshold_enabled", "similarity_threshold"}

    add_response = await test_client.post(
        f"/api/knowledge/knowledge-bases/{kb_id}/documents",
        json={"items": ["/tmp/demo.txt"], "params": {"content_type": "file"}},
        headers=admin_headers,
    )
    assert add_response.status_code == 400, add_response.text
    assert "只支持检索" in add_response.json()["detail"]

    parse_response = await test_client.post(
        f"/api/knowledge/knowledge-bases/{kb_id}/documents/parse",
        json=["file_id_1"],
        headers=admin_headers,
    )
    assert parse_response.status_code == 400, parse_response.text
    assert "只支持检索" in parse_response.json()["detail"]

    index_response = await test_client.post(
        f"/api/knowledge/knowledge-bases/{kb_id}/documents/index",
        json={"file_ids": ["file_id_1"], "params": {}},
        headers=admin_headers,
    )
    assert index_response.status_code == 400, index_response.text
    assert "只支持检索" in index_response.json()["detail"]


# =============================================================================
# === Knowledge Router Additional Tests ===
# =============================================================================


async def test_get_accessible_knowledge_bases(test_client, admin_headers, knowledge_base_resource):
    """测试获取可访问的知识库列表"""
    response = await test_client.get("/api/knowledge/knowledge-bases/accessible", headers=admin_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "knowledge_bases" in payload

    # 验证知识库在列表中
    kb_ids = [db["kb_id"] for db in payload["knowledge_bases"]]
    assert knowledge_base_resource["kb_id"] in kb_ids

    # 可访问列表补齐 stats，前端卡片才能展示真实文件数量，避免普通用户误显示 0
    matched = next(db for db in payload["knowledge_bases"] if db["kb_id"] == knowledge_base_resource["kb_id"])
    assert "stats" in matched
    assert "file_count" in matched["stats"]


async def test_create_knowledge_base_defaults_to_global_share_config(test_client, admin_headers):
    knowledge_base = await _create_test_knowledge_base(test_client, admin_headers)
    kb_id = knowledge_base["kb_id"]
    try:
        assert knowledge_base["share_config"] == {
            "version": 2,
            "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
            "manage_scope": None,
        }
    finally:
        await test_client.delete(f"/api/knowledge/knowledge-bases/{kb_id}", headers=admin_headers)


async def test_group_read_and_company_manage_scope_can_be_saved(test_client, admin_headers):
    """全集团可读时，管理范围可收窄到下级公司。"""

    group = await _create_test_department(test_client, admin_headers, "pytest_manage_group")
    company = await _create_test_department(test_client, admin_headers, "pytest_manage_company", parent_id=group["id"])
    database = None
    try:
        database = await _create_test_knowledge_base(
            test_client,
            admin_headers,
            {
                "version": 2,
                "read_scope": {
                    "access_level": "department",
                    "department_ids": [group["id"]],
                    "user_uids": [],
                },
                "manage_scope": {
                    "access_level": "department",
                    "department_ids": [company["id"]],
                    "user_uids": [],
                },
            },
        )

        assert database["share_config"]["manage_scope"]["department_ids"] == [company["id"]]
    finally:
        if database:
            await test_client.delete(f"/api/knowledge/knowledge-bases/{database['kb_id']}", headers=admin_headers)
        await _delete_department_with_admin(test_client, admin_headers, company)
        await _delete_department_with_admin(test_client, admin_headers, group)


@pytest.mark.parametrize(
    ("access_level", "scope_key"),
    [("department", "department_ids"), ("user", "user_uids")],
)
async def test_share_config_filters_accessible_knowledge_bases(test_client, admin_headers, access_level, scope_key):
    """按 share_config 的访问范围过滤可访问知识库，department 与 user 两级同构。"""
    department_a = await _create_test_department(test_client, admin_headers, "pytest_dept_a")
    department_b = await _create_test_department(test_client, admin_headers, "pytest_dept_b")
    user_a = user_b = None
    knowledge_base = None

    try:
        user_a = await _create_test_user(test_client, admin_headers, department_a["id"])
        user_b = await _create_test_user(test_client, admin_headers, department_b["id"])
        if access_level == "department":
            scope = {"access_level": "department", "department_ids": [department_a["id"]], "user_uids": []}
            scope_target = department_a["id"]
            manage_scope = scope
        else:
            scope = {"access_level": "user", "department_ids": [], "user_uids": [user_a["user"]["uid"]]}
            scope_target = user_a["user"]["uid"]

            manage_scope = scope
        knowledge_base = await _create_test_knowledge_base(
            test_client,
            admin_headers,
            {"version": 2, "read_scope": scope, "manage_scope": manage_scope},
        )

        saved_config = knowledge_base["share_config"]
        assert saved_config["manage_scope"]["access_level"] == access_level
        assert scope_target in saved_config["manage_scope"][scope_key]

        assert knowledge_base["kb_id"] in await _accessible_kb_ids(test_client, user_a["headers"])
        assert knowledge_base["kb_id"] not in await _accessible_kb_ids(test_client, user_b["headers"])
    finally:
        if knowledge_base:
            await test_client.delete(f"/api/knowledge/knowledge-bases/{knowledge_base['kb_id']}", headers=admin_headers)
        if user_a:
            await delete_user_by_id(test_client, admin_headers, user_a["user"]["id"])
        if user_b:
            await delete_user_by_id(test_client, admin_headers, user_b["user"]["id"])
        await _delete_department_with_admin(test_client, admin_headers, department_a)
        await _delete_department_with_admin(test_client, admin_headers, department_b)


async def test_department_share_config_inherits_to_subtree_and_isolates_siblings(test_client, admin_headers):
    departments = []
    user_a = user_b = None
    databases = []

    try:
        group = await _create_test_department(test_client, admin_headers, "pytest_group")
        departments.append(group)
        company_a = await _create_test_department(test_client, admin_headers, "pytest_company_a", parent_id=group["id"])
        departments.append(company_a)
        company_b = await _create_test_department(test_client, admin_headers, "pytest_company_b", parent_id=group["id"])
        departments.append(company_b)
        department_a = await _create_test_department(
            test_client,
            admin_headers,
            "pytest_dept_a",
            parent_id=company_a["id"],
        )
        departments.append(department_a)
        department_b = await _create_test_department(
            test_client,
            admin_headers,
            "pytest_dept_b",
            parent_id=company_b["id"],
        )
        departments.append(department_b)

        user_a = await _create_test_user(test_client, admin_headers, department_a["id"])
        user_b = await _create_test_user(test_client, admin_headers, department_b["id"])
        database_a = await _create_test_knowledge_base(
            test_client,
            admin_headers,
            _department_share_config(company_a["id"]),
        )
        databases.append(database_a)
        database_b = await _create_test_knowledge_base(
            test_client,
            admin_headers,
            _department_share_config(company_b["id"]),
        )
        databases.append(database_b)
        group_database = await _create_test_knowledge_base(
            test_client,
            admin_headers,
            _department_share_config(group["id"]),
        )
        databases.append(group_database)

        user_a_kb_ids = await _accessible_kb_ids(test_client, user_a["headers"])
        user_b_kb_ids = await _accessible_kb_ids(test_client, user_b["headers"])

        assert database_a["kb_id"] in user_a_kb_ids
        assert database_b["kb_id"] not in user_a_kb_ids
        assert group_database["kb_id"] in user_a_kb_ids

        assert database_b["kb_id"] in user_b_kb_ids
        assert database_a["kb_id"] not in user_b_kb_ids
        assert group_database["kb_id"] in user_b_kb_ids

        response = await test_client.get(
            f"/api/knowledge/knowledge-bases/{database_b['kb_id']}",
            headers=user_a["headers"],
        )
        _assert_not_found_response(response)
        response = await test_client.get(
            f"/api/knowledge/knowledge-bases/{database_a['kb_id']}",
            headers=user_b["headers"],
        )
        _assert_not_found_response(response)
    finally:
        for database in databases:
            await test_client.delete(f"/api/knowledge/knowledge-bases/{database['kb_id']}", headers=admin_headers)
        if user_a:
            await delete_user_by_id(test_client, admin_headers, user_a["user"]["id"])
        if user_b:
            await delete_user_by_id(test_client, admin_headers, user_b["user"]["id"])
        for department in reversed(departments):
            await _delete_department_with_admin(test_client, admin_headers, department)


async def test_get_knowledge_base_types(test_client, admin_headers):
    """测试获取支持的知识库类型"""
    response = await test_client.get("/api/knowledge/types", headers=admin_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["message"] == "success"
    assert "kb_types" in payload
    assert "default_config" not in payload["kb_types"]["dify"]
    assert payload["kb_types"]["dify"]["name"] == "Dify"
    assert payload["kb_types"]["dify"]["description"] == "连接 Dify Dataset 的只读检索知识库"
    assert payload["kb_types"]["dify"]["requires_embedding_model"] is False
    assert payload["kb_types"]["dify"]["supports_documents"] is False
    assert [option["key"] for option in payload["kb_types"]["dify"]["create_params"]["options"]] == [
        "dify_api_url",
        "dify_token",
        "dify_dataset_id",
    ]
    assert "default_config" not in payload["kb_types"]["notion"]
    assert payload["kb_types"]["notion"]["name"] == "Notion"
    assert payload["kb_types"]["notion"]["description"] == "连接 Notion Data Source 的只读知识库，支持检索"
    assert payload["kb_types"]["notion"]["requires_embedding_model"] is False
    assert payload["kb_types"]["notion"]["supports_documents"] is False
    assert [option["key"] for option in payload["kb_types"]["notion"]["create_params"]["options"]] == [
        "notion_token",
        "notion_data_source_id",
        "notion_version",
    ]


async def test_get_knowledge_base_statistics(test_client, admin_headers):
    """测试获取知识库统计信息"""
    response = await test_client.get("/api/knowledge/stats", headers=admin_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["message"] == "success"
    assert "stats" in payload


async def test_get_supported_file_types(test_client, admin_headers):
    """测试获取支持的文件类型"""
    response = await test_client.get("/api/knowledge/files/supported-types", headers=admin_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["message"] == "success"
    assert "file_types" in payload
    assert isinstance(payload["file_types"], list)


async def test_duplicate_knowledge_base_name(test_client, admin_headers, knowledge_base_resource):
    """测试重复创建同名知识库"""
    db_name = knowledge_base_resource["name"]
    response = await test_client.post(
        "/api/knowledge/knowledge-bases",
        json={
            "name": db_name,
            "description": "Duplicate name test",
            "embedding_model_spec": default_embedding_model_spec(),
            "kb_type": "milvus",
            "additional_params": {},
        },
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert "已存在" in response.json()["detail"]


async def test_create_lightrag_knowledge_base_is_unsupported(test_client, admin_headers):
    db_name = f"pytest_lightrag_{uuid.uuid4().hex[:6]}"
    response = await test_client.post(
        "/api/knowledge/knowledge-bases",
        json={
            "name": db_name,
            "description": "Unsupported LightRAG knowledge base",
            "embedding_model_spec": default_embedding_model_spec(),
            "kb_type": "lightrag",
            "additional_params": {},
        },
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert "Unsupported knowledge base type: lightrag" in response.json()["detail"]


async def test_sample_questions_endpoints(test_client, admin_headers, knowledge_base_resource):
    """测试示例问题接口（空文件时预期返回400）"""
    kb_id = knowledge_base_resource["kb_id"]

    # 获取示例问题（空知识库应该返回空列表）
    get_response = await test_client.get(f"/api/knowledge/knowledge-bases/{kb_id}/sample-questions", headers=admin_headers)
    assert get_response.status_code == 200, get_response.text
    get_payload = get_response.json()
    assert get_payload["kb_id"] == kb_id
    assert "questions" in get_payload
    assert get_payload["count"] == 0  # 空知识库没有问题

    # 生成示例问题（空知识库应该返回400）
    generate_response = await test_client.post(
        f"/api/knowledge/knowledge-bases/{kb_id}/sample-questions",
        json={"count": 5},
        headers=admin_headers,
    )
    assert generate_response.status_code == 400
    assert "中没有文件" in generate_response.json()["detail"]


@pytest.mark.parametrize(
    "search_params",
    [
        {},
        {"query": "nonexistent-needle-xyz", "offset": 0, "limit": 50},
    ],
)
async def test_document_search_returns_empty_results(test_client, admin_headers, knowledge_base_resource, search_params):
    """空关键词或不存在关键词都返回空结果，且不命中 /documents/{doc_id} 路由。"""
    kb_id = knowledge_base_resource["kb_id"]
    response = await test_client.get(
        f"/api/knowledge/knowledge-bases/{kb_id}/documents/search",
        params=search_params,
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["files"] == []
    assert payload["total"] == 0
    assert payload["has_more"] is False
    assert payload["offset"] == search_params.get("offset", 0)
    if "limit" in search_params:
        assert payload["limit"] == search_params["limit"]


async def test_document_search_allows_read_permission(test_client, standard_user, knowledge_base_resource):
    """具有读取功能权限且位于共享范围内的用户可以搜索文档。"""
    kb_id = knowledge_base_resource["kb_id"]
    response = await test_client.get(
        f"/api/knowledge/knowledge-bases/{kb_id}/documents/search",
        params={"query": "x"},
        headers=standard_user["headers"],
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload.get("files"), list)
    assert payload["offset"] == 0
    assert payload["limit"] == 100
    assert isinstance(payload["total"], int)
    assert payload["has_more"] is (payload["total"] > payload["limit"])
