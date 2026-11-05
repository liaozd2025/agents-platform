"""用真实 PostgreSQL 与 HTTP 验证迁移后的管理权限和 Memory 同步。"""

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from test.integration.support.module_audit import isolated_audit_store, serve_http
from yuxi.api.errors import domain_error_handler
from yuxi.api.routers.agents.management import agent_router
from yuxi.api.routers.identity.users import user_router
from yuxi.modules.agents.models.definitions import Agent
from yuxi.modules.agents.services.user_memory import PROFILE_START
from yuxi.modules.identity.models import User
from yuxi.modules.identity.security import AuthUtils
from yuxi.modules.workspace.filesystem import Workspace
from yuxi.shared.errors import DomainError

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


async def test_memory_preferences_immediately_publish_or_remove_profile(monkeypatch, tmp_path):
    """偏好提交后回读真实USER.md，停用时删除机器画像并保留手工内容。"""
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path / "user-data"))
    async with isolated_audit_store(monkeypatch) as (_manager, sessions):
        async with sessions() as db:
            user = User(uid="memory-owner", username="memory-owner", display_name="示例用户", password_hash="fixture")
            db.add(user)
            await db.commit()
            headers = {"Authorization": "Bearer " + AuthUtils.create_access_token({"sub": str(user.id), "auth_method": "password"})}
        app = FastAPI()
        app.add_exception_handler(DomainError, domain_error_handler)
        app.include_router(user_router, prefix="/api")
        async with serve_http(app) as client:
            enabled = await client.put("/api/user/config", headers=headers, json={"enable_memory": True})
            assert enabled.status_code == 200, enabled.text
            workspace = Workspace(user.uid)
            content = workspace.read_authorized_file("/agents/USER.md", 1024 * 1024).decode()
            assert PROFILE_START in content and "示例用户" in content
            workspace.replace_authorized_file("/agents/USER.md", (content + "\n手工沟通偏好\n").encode())
            disabled = await client.put("/api/user/config", headers=headers, json={"enable_memory": False})
            assert disabled.status_code == 200, disabled.text
            content = workspace.read_authorized_file("/agents/USER.md", 1024 * 1024).decode()
            assert PROFILE_START not in content and "示例用户" not in content
            assert "手工沟通偏好" in content
            assert (await client.get("/api/user/config", headers=headers)).json()["enable_memory"] is False


async def test_shared_agent_resource_manage_cannot_replace_function_permission(monkeypatch):
    """共享范围授予MANAGE但无agent:manage时，真实修改入口拒绝且数据库不变。"""
    async with isolated_audit_store(monkeypatch) as (_manager, sessions):
        async with sessions() as db:
            user = User(uid="reader", username="reader", password_hash="fixture")
            agent = Agent(
                slug="shared-guard",
                name="Original",
                backend_id="ChatbotAgent",
                visibility="shared",
                created_by="another",
                share_config={"version": 2, "read_scope": {"access_level": "global"}, "manage_scope": {"access_level": "global"}},
            )
            db.add_all([user, agent])
            await db.commit()
            headers = {"Authorization": "Bearer " + AuthUtils.create_access_token({"sub": str(user.id), "auth_method": "password"})}
        app = FastAPI()
        app.add_exception_handler(DomainError, domain_error_handler)
        app.include_router(agent_router, prefix="/api")
        async with serve_http(app) as client:
            result = await client.put("/api/agent/shared-guard", headers=headers, json={"name": "Changed"})
            assert result.status_code == 403, result.text
        async with sessions() as db:
            saved = await db.scalar(select(Agent).where(Agent.slug == "shared-guard"))
            assert saved.name == "Original"
