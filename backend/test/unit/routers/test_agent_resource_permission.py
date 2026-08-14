import importlib
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.utils.auth_middleware import get_authorization_context, get_db
from yuxi.agents.context import BaseContext
from yuxi.repositories.agent_repository import user_can_access_agent

agent_router = importlib.import_module("server.routers.agent_router")


def test_agent_manage_combines_function_permission_and_resource_scope(monkeypatch):
    """功能权限缺失返回 403，超出资源范围返回 404。"""

    state = {"permissions": set(), "can_manage_resource": False}
    user = SimpleNamespace(uid="manager", role="user", department_id=None)
    item = SimpleNamespace(
        slug="shared-agent",
        backend_id="ChatbotAgent",
        name="Shared Agent",
        config_json={"context": {}},
        created_by="owner",
        share_config={
            "version": 2,
            "read_scope": {"access_level": "global"},
            "manage_scope": {"access_level": "user", "user_uids": ["owner"]},
        },
    )

    async def fake_authorization():
        return SimpleNamespace(
            user=user,
            has_permission=lambda permission: permission in state["permissions"],
        )

    async def fake_db():
        yield object()

    class FakeRepository:
        def __init__(self, _db):
            pass

        async def get_visible_by_slug(self, **_kwargs):
            item.share_config["manage_scope"] = (
                {"access_level": "global"}
                if state["can_manage_resource"]
                else {"access_level": "user", "user_uids": ["owner"]}
            )
            return item if user_can_access_agent(user, item) else None

        async def update(self, agent, **_kwargs):
            return agent

        async def serialize(self, agent, *, can_manage, **_kwargs):
            return {
                "slug": agent.slug,
                "backend_id": agent.backend_id,
                "config_json": agent.config_json,
                "can_manage": can_manage,
            }

    backend = SimpleNamespace(context_schema=BaseContext)
    monkeypatch.setattr(agent_router, "AgentRepository", FakeRepository)
    monkeypatch.setattr(agent_router.agent_manager, "get_agent", lambda _backend_id: backend)

    app = FastAPI()
    app.include_router(agent_router.agent_router)
    app.dependency_overrides[get_authorization_context] = fake_authorization
    app.dependency_overrides[get_db] = fake_db
    client = TestClient(app)

    assert client.put("/agent/shared-agent", json={"name": "Updated"}).status_code == 403
    state["permissions"] = {"agent:manage"}
    assert client.put("/agent/shared-agent", json={"name": "Updated"}).status_code == 404
    state["can_manage_resource"] = True
    response = client.put("/agent/shared-agent", json={"name": "Updated"})
    assert response.status_code == 200
    assert response.json()["agent"]["can_manage"] is True


def test_agent_run_requires_use_permission():
    user = SimpleNamespace(uid="manager")

    async def fake_authorization():
        return SimpleNamespace(
            user=user,
            has_permission=lambda permission: permission == "agent:manage",
        )

    app = FastAPI()
    app.include_router(agent_router.agent_router)
    app.dependency_overrides[get_authorization_context] = fake_authorization

    response = TestClient(app).post(
        "/agent/runs",
        json={"query": "hello", "agent_slug": "shared-agent", "thread_id": "thread-1"},
    )

    assert response.status_code == 403
