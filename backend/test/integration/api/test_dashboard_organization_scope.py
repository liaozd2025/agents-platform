"""新版 Session/Message 的组织快照必须经真实 PostgreSQL 与 HTTP 保持隔离。"""

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete

from test.integration.support.module_audit import isolated_audit_store, serve_http
from yuxi.api.errors import domain_error_handler
from yuxi.api.routers.dashboard import dashboard
from yuxi.api.routers.knowledge.dashboard import knowledge_dashboard
from yuxi.modules.agents.models.definitions import Agent
from yuxi.modules.agents.models.messages import Message
from yuxi.modules.agents.repositories.messages import MessageRepository
from yuxi.modules.agents.repositories.sessions import SessionRepository
from yuxi.modules.identity.models import Department, Role, RolePermission, User, UserRoleAssignment
from yuxi.modules.identity.security import AuthUtils
from yuxi.modules.knowledge.models import KnowledgeBase
from yuxi.modules.workspace.models import Project
from yuxi.shared.datetime import utc_now
from yuxi.shared.errors import DomainError

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


@pytest_asyncio.fixture
async def scoped_dashboard(monkeypatch):
    """所有用户、组织、历史事件和资源只存在于本例独立 Schema。"""
    async with isolated_audit_store(monkeypatch) as (_manager, sessions):
        async with sessions() as db:
            first = Department(name="Scope A")
            second = Department(name="Scope B")
            db.add_all([first, second])
            await db.flush()
            first.path = f"/{first.id}/"
            second.path = f"/{second.id}/"
            role = Role(
                code="dashboard-test",
                name="Dashboard manager",
                default_scope_type="organization_and_descendants",
                permissions=[RolePermission(permission_key="dashboard:view")],
            )
            managers = [
                User(uid=f"manager-{index}", username=f"manager-{index}", password_hash="fixture", department=department)
                for index, department in enumerate((first, second))
            ]
            member = User(uid="scope-member", username="scope-member", password_hash="fixture", department=first)
            agent = Agent(slug="scope-agent", name="Scope agent", backend_id="chatbot", visibility="shared", share_config={})
            db.add_all([role, *managers, member, agent])
            await db.flush()
            db.add_all([UserRoleAssignment(user=manager, role=role, scope_mode="inherit") for manager in managers])
            db.add(
                Project(
                    id="scope-project",
                    uid=member.uid,
                    selection_status="implicit",
                    workdir_path="test/dashboard-scope",
                    directory_mode="managed",
                )
            )
            await db.flush()
            thread = await SessionRepository(db).add_session(
                uid=member.uid, agent_id=agent.slug, thread_id="scope-thread", project_id="scope-project"
            )
            await MessageRepository(db).add_message(thread.id, "assistant", "A 的历史内容")
            now = utc_now()
            db.add(
                Message(
                    session_record_id=thread.id,
                    role="tool",
                    content="",
                    message_type="tool_audit",
                    started_at=now,
                    execution_status="completed",
                    extra_metadata={"tool_name": "before-transfer"},
                    organization_id_snapshot=first.id,
                    organization_path_snapshot=first.path,
                )
            )
            await db.commit()
            headers = [
                {"Authorization": "Bearer " + AuthUtils.create_access_token({"sub": str(user.id), "auth_method": "password"})}
                for user in [*managers, member]
            ]
            facts = {
                "member_id": member.id,
                "thread_record_id": thread.id,
                "a_id": first.id,
                "a_path": first.path,
                "b_id": second.id,
                "b_path": second.path,
            }
        app = FastAPI()
        app.add_exception_handler(DomainError, domain_error_handler)
        app.include_router(dashboard, prefix="/api")
        app.include_router(knowledge_dashboard, prefix="/api")
        async with serve_http(app) as client:
            yield client, sessions, headers, facts


async def test_history_scope_survives_transfer_and_cannot_be_broadened(scoped_dashboard):
    client, sessions, headers, facts = scoped_dashboard
    async with sessions() as db:
        member = await db.get(User, facts["member_id"])
        member.department_id = facts["b_id"]
        now = utc_now()
        db.add(
            Message(
                session_record_id=facts["thread_record_id"],
                role="tool",
                content="",
                message_type="tool_audit",
                started_at=now,
                execution_status="completed",
                extra_metadata={"tool_name": "after-transfer"},
                organization_id_snapshot=facts["b_id"],
                organization_path_snapshot=facts["b_path"],
                organization_snapshot_inferred=True,
            )
        )
        await db.commit()

    a_tools = await client.get("/api/dashboard/stats/tools", headers=headers[0])
    b_tools = await client.get("/api/dashboard/stats/tools", headers=headers[1])
    assert a_tools.status_code == b_tools.status_code == 200
    assert a_tools.json()["most_used_tools"] == [{"tool_name": "before-transfer", "count": 1}]
    assert b_tools.json()["most_used_tools"] == [{"tool_name": "after-transfer", "count": 1}]
    assert (await client.get("/api/dashboard/stats/current-organization", headers=headers[0])).json()["total_users"] == 1
    assert (await client.get("/api/dashboard/stats/current-organization", headers=headers[1])).json()["total_users"] == 2
    threads = await client.get("/api/dashboard/stats/threads", headers=headers[0])
    assert threads.status_code == 200, threads.text
    assert threads.json()["summary"]["total_threads"] == 1
    assert (await client.get("/api/dashboard/stats", headers=headers[0])).json()["contains_inferred_data"] is False
    assert (await client.get("/api/dashboard/stats", headers=headers[1])).json()["contains_inferred_data"] is True
    assert (await client.get("/api/dashboard/sessions/scope-thread", headers=headers[0])).status_code == 200
    assert (await client.get("/api/dashboard/sessions/scope-thread", headers=headers[1])).status_code == 404
    assert (await client.get(f"/api/dashboard/stats/tools?department_id={facts['b_id']}", headers=headers[0])).status_code == 404
    assert (await client.get("/api/dashboard/sessions", headers=headers[2])).status_code == 403


async def test_resource_creation_and_shared_visibility_use_distinct_evidence(scoped_dashboard):
    client, sessions, headers, facts = scoped_dashboard
    async with sessions() as db:
        db.add(
            KnowledgeBase(
                kb_id="scope-kb",
                name="Scope knowledge",
                kb_type="milvus",
                created_by="scope-member",
                share_config={
                    "version": 2,
                    "read_scope": {"access_level": "department", "department_ids": [facts["b_id"]], "user_uids": []},
                    "manage_scope": None,
                },
                organization_id_snapshot=facts["a_id"],
                organization_path_snapshot=facts["a_path"],
            )
        )
        await db.commit()
    try:
        first = await client.get("/api/dashboard/stats/resources", headers=headers[0])
        second = await client.get("/api/dashboard/stats/resources", headers=headers[1])
        assert first.status_code == second.status_code == 200
        assert first.json()["knowledge_bases"]["creation_count"] == 1
        assert second.json()["knowledge_bases"]["creation_count"] == 0
        assert second.json()["knowledge_bases"]["shared_visible_count"] == 1
        assert (await client.get("/api/dashboard/stats/knowledge", headers=headers[1])).json()["total_knowledge_bases"] == 1
    finally:
        # 此例直接写元数据，未发布缓存；回收自己的行避免依赖无关 Redis 服务。
        async with sessions() as db:
            await db.execute(delete(KnowledgeBase).where(KnowledgeBase.kb_id == "scope-kb"))
            await db.commit()


async def test_dashboard_json_statistics_tolerate_nul_without_changing_audits_or_scope(scoped_dashboard):
    """实际 PG/HTTP 保留含空字符的历史审计，并正确聚合工具与模型统计。"""
    client, sessions, headers, facts = scoped_dashboard
    tool_cases = [
        ("nul-output", "completed", {"content": "binary\0payload"}),
        (r"literal-\u0000", "completed", {"content": r"literal \u0000 survives"}),
        ('quoted-工具"name', "completed", {"nested": ["slash\\\0\0end", "😀"]}),
        ("failed-nul", "failed", {"content": "\0\0"}),
    ]
    ids = []
    async with sessions() as db:
        for name, status, output in tool_cases:
            item = Message(
                session_record_id=facts["thread_record_id"],
                role="tool",
                content="",
                message_type="tool_audit",
                started_at=utc_now(),
                execution_status=status,
                extra_metadata={"tool_name": name, "output": output},
                organization_id_snapshot=facts["a_id"],
                organization_path_snapshot=facts["a_path"],
            )
            db.add(item)
            await db.flush()
            ids.append(item.id)
        db.add(
            Message(
                session_record_id=facts["thread_record_id"],
                role="tool",
                content="",
                message_type="tool_audit",
                started_at=utc_now(),
                execution_status="completed",
                extra_metadata={"tool_name": "other-scope-tool", "output": "\0"},
                organization_id_snapshot=facts["b_id"],
                organization_path_snapshot=facts["b_path"],
            )
        )
        db.add(
            Message(
                session_record_id=facts["thread_record_id"],
                role="assistant",
                content="answer",
                message_type="text",
                extra_metadata={"response_metadata": {"model_name": "fixture-model"}, "raw_output": "\0"},
                organization_id_snapshot=facts["a_id"],
                organization_path_snapshot=facts["a_path"],
            )
        )
        await db.commit()
    response = await client.get("/api/dashboard/stats/tools", headers=headers[0])
    assert response.status_code == 200, response.text
    stats = response.json()
    assert stats["total_calls"] == 5
    assert stats["successful_calls"] == 4
    assert stats["failed_calls"] == 1
    assert {item["tool_name"]: item["count"] for item in stats["most_used_tools"]} == {
        "before-transfer": 1,
        **{name: 1 for name, _, _ in tool_cases},
    }
    for kind in ["tools", "models"]:
        series = await client.get(
            "/api/dashboard/stats/calls/timeseries", params={"type": kind, "time_range": "14days"}, headers=headers[0]
        )
        assert series.status_code == 200, series.text
        assert "other-scope-tool" not in series.text
        if kind == "tools":
            assert set(series.json()["categories"]) == {"before-transfer", *(name for name, _, _ in tool_cases)}
        else:
            assert "fixture-model" in series.text
    other = await client.get("/api/dashboard/stats/tools", headers=headers[1])
    assert other.status_code == 200
    assert other.json()["total_calls"] == 1
    assert other.json()["most_used_tools"] == [{"tool_name": "other-scope-tool", "count": 1}]
    async with sessions() as db:
        for mid, (name, _, output) in zip(ids, tool_cases, strict=True):
            stored = await db.get(Message, mid)
            assert stored.extra_metadata == {"tool_name": name, "output": output}
