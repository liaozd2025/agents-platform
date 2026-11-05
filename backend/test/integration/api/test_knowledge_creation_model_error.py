"""创建知识库时失效模型的错误反馈及持久化边界。"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from test.integration.support.module_audit import isolated_audit_store, serve_http
from yuxi.api.errors import domain_error_handler
from yuxi.api.routers.knowledge import management
from yuxi.modules.identity.models import User
from yuxi.modules.knowledge.manager import KnowledgeBaseManager
from yuxi.modules.knowledge.models import KnowledgeBase
from yuxi.modules.models.providers.cache import model_cache
from yuxi.shared.errors import DomainError

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


@pytest.mark.parametrize("invalid_model", [None, "removed:embedding", "fixture:chat"])
async def test_stale_embedding_returns_actionable_error_then_valid_selection_creates(monkeypatch, tmp_path, invalid_model):
    """真实 HTTP 拒绝过期模型且不落库，改选可用模型后回读正确绑定。"""
    valid_model = "fixture:embedding"
    monkeypatch.setattr(
        model_cache,
        "get_model_info",
        lambda spec: {valid_model: SimpleNamespace(model_type="embedding"), "fixture:chat": SimpleNamespace(model_type="chat")}.get(spec),
    )
    manager = KnowledgeBaseManager(str(tmp_path))
    monkeypatch.setattr(management, "knowledge_base_manager", manager)
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.include_router(management.knowledge, prefix="/api")
    app.dependency_overrides[management.require_knowledge_base_manage_permission] = lambda: SimpleNamespace(
        uid="creator", department_id=None
    )
    async with isolated_audit_store(monkeypatch) as (_, sessions):
        async with sessions() as db:
            db.add(User(uid="creator", username="creator", password_hash="fixture"))
            await db.commit()
        payload = {
            "name": "model-selection-regression",
            "description": "",
            "kb_type": "milvus",
            "embedding_model_spec": invalid_model,
            "share_config": {"version": 2, "read_scope": {"access_level": "global"}, "manage_scope": None},
        }
        async with serve_http(app) as client:
            failed = await client.post("/api/knowledge/knowledge-bases", json=payload)
            assert failed.status_code == 400
            assert failed.json() == {
                "detail": {
                    "code": "embedding_model_unavailable" if invalid_model else "embedding_model_required",
                    "message": "嵌入模型已停用或不存在，请重新选择可用的嵌入模型" if invalid_model else "请选择嵌入模型",
                }
            }
            async with sessions() as db:
                assert await db.scalar(select(func.count()).select_from(KnowledgeBase)) == 0
            payload["embedding_model_spec"] = valid_model
            created = await client.post("/api/knowledge/knowledge-bases", json=payload)
            assert created.status_code == 200, created.text
            async with sessions() as db:
                saved = await db.scalar(select(KnowledgeBase).where(KnowledgeBase.kb_id == created.json()["kb_id"]))
                assert saved is not None
                assert saved.name == payload["name"]
                assert saved.embedding_model_spec == valid_model
