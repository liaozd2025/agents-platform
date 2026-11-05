"""Agent 管理 HTTP 入口同时执行功能权限和资源范围。"""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from yuxi.api.dependencies.auth import get_db, get_required_user
from yuxi.api.routers.agents import management as agent_router
from yuxi.modules.agents.permissions import user_can_access_agent, user_can_manage_agent


def test_agent_manage_combines_function_permission_and_resource_scope(monkeypatch):
    """功能权限缺失返回403，管理资源范围不匹配返回404，授权后才更新。"""
    state = {"permissions": set(), "can_manage_resource": False}
    user = SimpleNamespace(uid="manager", department_id=None, user_kind="human", is_deleted=False, role_assignments=[])
    item = SimpleNamespace(
        slug="shared-agent",
        visibility="shared",
        is_builtin=False,
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
    updates = []

    def current_user():
        role = SimpleNamespace(
            is_active=True,
            default_scope_type="all",
            default_departments=[],
            permissions=[SimpleNamespace(permission_key=key) for key in state["permissions"]],
        )
        user.role_assignments = [SimpleNamespace(role=role, scope_mode="inherit")]
        return user

    async def visible(_db, **kwargs):
        item.share_config["manage_scope"] = (
            {"access_level": "global"} if state["can_manage_resource"] else {"access_level": "user", "user_uids": ["owner"]}
        )
        return item if user_can_access_agent(user, item) else None

    async def update(_db, agent, **kwargs):
        updates.append(kwargs)
        return agent

    async def serialize(_db, agent, **kwargs):
        return {"slug": agent.slug, "can_manage": user_can_manage_agent(user, agent)}

    monkeypatch.setattr(agent_router, "get_visible_agent_definition", visible)
    monkeypatch.setattr(agent_router, "update_agent_definition", update)
    monkeypatch.setattr(agent_router, "serialize_agent_definition", serialize)
    app = FastAPI()
    app.include_router(agent_router.agent_router)
    app.dependency_overrides[get_required_user] = current_user
    app.dependency_overrides[get_db] = lambda: object()
    client = TestClient(app)
    assert client.put("/agent/shared-agent", json={"name": "Updated"}).status_code == 403
    state["permissions"] = {"agent:manage"}
    assert client.put("/agent/shared-agent", json={"name": "Updated"}).status_code == 404
    assert updates == []
    state["can_manage_resource"] = True
    response = client.put("/agent/shared-agent", json={"name": "Updated"})
    assert response.status_code == 200
    assert response.json()["agent"]["can_manage"] is True
    assert len(updates) == 1
