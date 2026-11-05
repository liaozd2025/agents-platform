from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

import yuxi.api.routers.knowledge.management as knowledge_router
from yuxi.api.dependencies.auth import get_authorization_context
from yuxi.api.responses.knowledge import serialize_knowledge_base
from yuxi.modules.knowledge.read_models import KnowledgeBaseDetail, KnowledgeBaseSummary


def test_knowledge_read_combines_function_permission_and_resource_scope(monkeypatch):
    """功能权限缺失返回 403，越出资源范围返回 404。"""

    state = {"permissions": set(), "shared": True}
    user = SimpleNamespace(uid="reader", department_id=2)

    async def fake_authorization():
        return SimpleNamespace(
            user=user,
            has_permission=lambda permission: permission in state["permissions"],
        )

    async def fake_get_knowledge_base_info(_kb_id):
        read_scope = {"access_level": "global"} if state["shared"] else {"access_level": "user", "user_uids": ["another-user"]}
        return {"created_by": "owner", "share_config": {"version": 2, "read_scope": read_scope}}

    monkeypatch.setattr(knowledge_router.knowledge_base_manager, "get_knowledge_base_info", fake_get_knowledge_base_info)
    app = FastAPI()

    @app.get("/knowledge/{kb_id}")
    async def read_knowledge(_current_user=Depends(knowledge_router.require_knowledge_base_read)):
        return {"ok": True}

    app.dependency_overrides[get_authorization_context] = fake_authorization
    client = TestClient(app)

    assert client.get("/knowledge/kb-1").status_code == 403
    state.update(permissions={"knowledge_base:read"}, shared=False)
    assert client.get("/knowledge/kb-1").status_code == 404
    state["shared"] = True
    assert client.get("/knowledge/kb-1").status_code == 200
    state["permissions"] = {"knowledge_base:manage"}
    assert client.get("/knowledge/kb-1").status_code == 200


def test_serialize_knowledge_base_redacts_credentials_from_compatibility_fields():
    database = KnowledgeBaseSummary(
        kb_id="kb-1",
        name="知识库",
        description=None,
        kb_type="dify",
        embedding_model_spec=None,
        llm_model_spec=None,
        query_params={},
        additional_params={"dify_token": "secret", "chunk_size": 100},
        share_config={"version": 2, "read_scope": None, "manage_scope": None},
        created_by=None,
        created_at=None,
    )

    response = serialize_knowledge_base(database, redact_secrets=True)

    assert response["additional_params"]["chunk_size"] == 100
    assert response["metadata"]["chunk_size"] == 100
    assert "dify_token" not in response["additional_params"]
    assert "dify_token" not in response["metadata"]


@pytest.mark.asyncio
async def test_database_detail_only_exposes_credentials_with_function_and_resource_manage(monkeypatch):
    database = KnowledgeBaseDetail(
        kb_id="kb-1",
        name="知识库",
        description=None,
        kb_type="dify",
        embedding_model_spec=None,
        llm_model_spec=None,
        query_params={},
        additional_params={"dify_token": "secret"},
        share_config={
            "version": 2,
            "read_scope": {"access_level": "global"},
            "manage_scope": {"access_level": "global"},
        },
        created_by="owner",
        created_at=None,
    )

    async def fake_get_knowledge_base_info(_kb_id, include_files=False):
        return database

    monkeypatch.setattr(knowledge_router.knowledge_base_manager, "get_knowledge_base_info", fake_get_knowledge_base_info)
    user = SimpleNamespace(uid="reader", department_id=None)

    readonly = await knowledge_router.get_knowledge_base_info(
        "kb-1",
        False,
        user,
        SimpleNamespace(has_permission=lambda permission: permission == "knowledge_base:read"),
    )
    manager = await knowledge_router.get_knowledge_base_info(
        "kb-1",
        False,
        user,
        SimpleNamespace(has_permission=lambda permission: permission == "knowledge_base:manage"),
    )

    assert "dify_token" not in readonly["additional_params"]
    assert manager["additional_params"]["dify_token"] == "secret"


@pytest.mark.asyncio
async def test_readonly_admin_can_read_but_cannot_update_knowledge_base(monkeypatch):
    admin = SimpleNamespace(uid="admin-1", department_id=2)

    database = {
        "created_by": "owner",
        "share_config": {
            "version": 2,
            "read_scope": {"access_level": "global"},
            "manage_scope": None,
        },
    }

    async def fake_get_knowledge_base_info(_kb_id):
        return database

    monkeypatch.setattr(knowledge_router.knowledge_base_manager, "get_knowledge_base_info", fake_get_knowledge_base_info)

    assert await knowledge_router.require_knowledge_base_read("kb-1", admin) is admin

    with pytest.raises(HTTPException) as exc_info:
        await knowledge_router.require_knowledge_base_manage("kb-1", admin)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_regular_user_cannot_manage_knowledge_base(monkeypatch):
    async def fake_get_knowledge_base_info(_kb_id):
        return {
            "created_by": "owner",
            "share_config": {
                "version": 2,
                "read_scope": {"access_level": "global"},
                "manage_scope": None,
            },
        }

    monkeypatch.setattr(knowledge_router.knowledge_base_manager, "get_knowledge_base_info", fake_get_knowledge_base_info)
    owner = SimpleNamespace(uid="other-user", department_id=2)

    with pytest.raises(HTTPException) as exc_info:
        await knowledge_router.require_knowledge_base_manage("kb-1", owner)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_query_parameter_routes_apply_knowledge_base_acl(monkeypatch):
    database = {
        "created_by": "owner",
        "share_config": {
            "version": 2,
            "read_scope": {"access_level": "user", "user_uids": ["admin-1"]},
            "manage_scope": None,
        },
    }

    async def fake_get_knowledge_base_info(_kb_id):
        return database

    monkeypatch.setattr(knowledge_router.knowledge_base_manager, "get_knowledge_base_info", fake_get_knowledge_base_info)
    readonly_admin = SimpleNamespace(uid="admin-1", department_id=2)

    assert await knowledge_router.require_knowledge_base_read("kb-1", readonly_admin) is readonly_admin

    with pytest.raises(HTTPException) as exc_info:
        await knowledge_router.require_knowledge_base_read("kb-1", SimpleNamespace(uid="admin-2", department_id=2))
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_list_redacts_credentials_when_resource_manage_has_no_function_manage(monkeypatch):
    """列表不能绕过详情的功能权限脱敏边界。"""
    from unittest.mock import AsyncMock

    from yuxi.modules.identity.permissions import ResourcePermission

    summary = KnowledgeBaseSummary(
        kb_id="kb-1",
        name="Private connector",
        description=None,
        kb_type="dify",
        embedding_model_spec=None,
        llm_model_spec=None,
        query_params={},
        additional_params={"dify_token": "secret"},
        share_config={"version": 2, "read_scope": {"access_level": "global"}, "manage_scope": {"access_level": "global"}},
        created_by="reader",
        created_at=None,
        effective_permission=ResourcePermission.MANAGE,
    )
    monkeypatch.setattr(knowledge_router.knowledge_base_manager, "get_knowledge_bases_by_uid", AsyncMock(return_value=[summary]))
    response = await knowledge_router.get_knowledge_bases(SimpleNamespace(uid="reader"), SimpleNamespace(has_permission=lambda _: False))
    item = response["knowledge_bases"][0]
    assert "dify_token" not in item["additional_params"]
    assert "dify_token" not in item["metadata"]
    assert item["can_manage"] is False
