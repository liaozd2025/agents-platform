"""
Integration tests for dashboard router endpoints.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from yuxi.agents.skills.repository import SkillRepository
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import (
    ROOT_DEPARTMENT_ID,
    Agent,
    AgentRun,
    Department,
    Conversation,
    ConversationStats,
    Message,
    MessageFeedback,
    OperationLog,
    Project,
    Role,
    RolePermission,
    Skill,
    ToolCall,
    User,
    UserRoleAssignment,
)
from yuxi.storage.postgres.models_knowledge import KnowledgeBase
from yuxi.utils.auth_utils import AuthUtils
from yuxi.config.runtime import knowledge_capability_enabled
from yuxi.utils.datetime_utils import utc_now_naive

from test.live_api_cleanup import make_test_conversation_metadata, make_test_conversation_title

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def dashboard_scope_users(test_client):
    """创建两个兄弟管理域的 Dashboard 测试账号。"""

    pg_manager.initialize()
    await pg_manager.async_engine.dispose()
    await pg_manager.create_tables()
    await pg_manager.ensure_business_schema()

    suffix = uuid.uuid4().hex[:10]
    password = f"Pw!{uuid.uuid4().hex}"
    async with pg_manager.get_async_session_context() as session:
        root = await session.get(Department, ROOT_DEPARTMENT_ID)
        user_role = await session.scalar(select(Role).where(Role.code == "user"))
        assert root is not None and user_role is not None

        department_a = Department(name=f"pytest-dashboard-a-{suffix}", parent_id=root.id)
        department_b = Department(name=f"pytest-dashboard-b-{suffix}", parent_id=root.id)
        session.add_all([department_a, department_b])
        await session.flush()
        department_a.path = f"{root.path}{department_a.id}/"
        department_b.path = f"{root.path}{department_b.id}/"
        department_child = Department(name=f"pytest-dashboard-child-{suffix}", parent_id=department_a.id)
        session.add(department_child)
        await session.flush()
        department_child.path = f"{department_a.path}{department_child.id}/"

        role = Role(
            code=f"pytest_dashboard_{suffix}",
            name="Dashboard 集成测试角色",
            description="",
            is_active=True,
            default_scope_type="organization_and_descendants",
            permissions=[RolePermission(permission_key="dashboard:view")],
        )
        users = [
            User(
                username=f"Dashboard A {suffix}",
                uid=f"pytest_dashboard_a_{suffix}",
                password_hash=AuthUtils.hash_password(password),
                department=department_a,
            ),
            User(
                username=f"Dashboard B {suffix}",
                uid=f"pytest_dashboard_b_{suffix}",
                password_hash=AuthUtils.hash_password(password),
                department=department_b,
            ),
            User(
                username=f"Dashboard Child {suffix}",
                uid=f"pytest_dashboard_child_{suffix}",
                password_hash=AuthUtils.hash_password(password),
                department=department_child,
            ),
        ]
        session.add_all([role, *users])
        await session.flush()
        session.add_all(
            [
                UserRoleAssignment(user=users[0], role=role, scope_mode="inherit"),
                UserRoleAssignment(user=users[1], role=role, scope_mode="inherit"),
                UserRoleAssignment(user=users[2], role=user_role, scope_mode="inherit"),
            ]
        )
        user_ids = [user.id for user in users]
        user_uids = [user.uid for user in users]
        department_ids = [department_child.id, department_a.id, department_b.id]
        role_id = role.id
        department_child_path = department_child.path
        department_b_path = department_b.path

    headers = []
    for user in users:
        response = await test_client.post("/api/auth/token", data={"username": user.uid, "password": password})
        assert response.status_code == 200, response.text
        headers.append({"Authorization": f"Bearer {response.json()['access_token']}"})

    try:
        yield {
            "a": headers[0],
            "b": headers[1],
            "without_permission": headers[2],
            "department_b": department_b.id,
            "department_child": department_child.id,
            "department_child_path": department_child_path,
            "department_b_path": department_b_path,
            "manager_a_id": user_ids[0],
            "manager_a_uid": user_uids[0],
            "manager_b_id": user_ids[1],
            "manager_b_uid": user_uids[1],
            "child_user_id": user_ids[2],
            "child_user_uid": user_uids[2],
        }
    finally:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.created_by.in_(user_uids)))
            await session.execute(delete(Agent).where(Agent.created_by.in_(user_uids)))
            await session.execute(delete(Skill).where(Skill.created_by.in_(user_uids)))
            conversation_ids = list(
                await session.scalars(select(Conversation.id).where(Conversation.uid.in_(user_uids)))
            )
            if conversation_ids:
                await session.execute(delete(AgentRun).where(AgentRun.conversation_id.in_(conversation_ids)))
                message_ids = list(
                    await session.scalars(select(Message.id).where(Message.conversation_id.in_(conversation_ids)))
                )
                if message_ids:
                    await session.execute(delete(ToolCall).where(ToolCall.message_id.in_(message_ids)))
                    await session.execute(delete(MessageFeedback).where(MessageFeedback.message_id.in_(message_ids)))
                    await session.execute(delete(Message).where(Message.id.in_(message_ids)))
                await session.execute(
                    delete(ConversationStats).where(ConversationStats.conversation_id.in_(conversation_ids))
                )
                await session.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
            await session.execute(delete(Project).where(Project.uid.in_(user_uids)))
            await session.execute(delete(OperationLog).where(OperationLog.user_id.in_(user_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
            await session.execute(delete(Role).where(Role.id == role_id))
            for department_id in department_ids:
                await session.execute(delete(Department).where(Department.id == department_id))
        await pg_manager.async_engine.dispose()


async def _set_conversation_statuses(subagent_thread_id: str, deleted_thread_id: str) -> None:
    """使用绑定当前测试事件循环的一次性引擎写入状态事实。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    try:
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with session_factory() as db:
            await db.execute(
                update(Conversation).where(Conversation.thread_id == subagent_thread_id).values(status="subagent")
            )
            await db.execute(
                update(Conversation).where(Conversation.thread_id == deleted_thread_id).values(status="deleted")
            )
            await db.commit()
    finally:
        await engine.dispose()


async def test_dashboard_requires_authentication(test_client):
    response = await test_client.get("/api/dashboard/conversations")
    assert response.status_code == 401


async def test_standard_user_is_forbidden(test_client, standard_user):
    response = await test_client.get("/api/dashboard/conversations", headers=standard_user["headers"])
    assert response.status_code == 403


async def test_admin_can_fetch_conversations(test_client, admin_headers):
    response = await test_client.get("/api/dashboard/conversations", headers=admin_headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert set(data) == {"items", "total", "limit", "offset"}
    assert isinstance(data["items"], list)
    assert data["total"] >= len(data["items"])


async def test_admin_can_fetch_conversation_filter_options(test_client, admin_headers):
    response = await test_client.get("/api/dashboard/conversations/options", headers=admin_headers)

    assert response.status_code == 200, response.text
    data = response.json()
    assert set(data) == {"users", "agents"}
    assert all("is_deleted" in item for item in data["users"])
    assert all("is_deleted" in item for item in data["agents"])


async def test_dashboard_conversation_audit_reports_latest_run_status(test_client, dashboard_scope_users):
    """会话生命周期保持 active 时，审计接口仍返回最新 Run 的真实终态。"""
    project_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    marker = f"dashboard-run-status-{uuid.uuid4().hex}"
    now = utc_now_naive()

    async with pg_manager.get_async_session_context() as session:
        session.add(
            Project(
                id=project_id,
                uid=dashboard_scope_users["manager_a_uid"],
                selection_status="implicit",
                workdir_path=f"projects/{project_id}",
                directory_mode="managed",
            )
        )
        await session.flush()
        conversation = await ConversationRepository(session).add_conversation(
            uid=dashboard_scope_users["manager_a_uid"],
            agent_id="pytest-dashboard-run-status-agent",
            title=marker,
            thread_id=thread_id,
            project_id=project_id,
        )
        session.add(
            AgentRun(
                id=str(uuid.uuid4()),
                conversation_thread_id=thread_id,
                runtime_scope_id=thread_id,
                agent_slug=conversation.agent_id,
                uid=conversation.uid,
                status="completed",
                request_id=str(uuid.uuid4()),
                conversation_id=conversation.id,
                run_type="chat",
                input_payload={},
                created_at=now,
                finished_at=now,
            )
        )
        await session.commit()

    response = await test_client.get(
        "/api/dashboard/conversations",
        params={"search": marker, "status": "active"},
        headers=dashboard_scope_users["a"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    item = response.json()["items"][0]
    assert item["status"] == "active"
    assert item["run_status"] == "completed"

    detail = await test_client.get(
        f"/api/dashboard/conversations/{thread_id}",
        headers=dashboard_scope_users["a"],
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == "active"
    assert detail.json()["run_status"] == "completed"


async def test_dashboard_rejects_invalid_query_ranges(test_client, admin_headers):
    responses = [
        await test_client.get("/api/dashboard/stats/threads?time_range=365days", headers=admin_headers),
        await test_client.get("/api/dashboard/conversations?limit=0", headers=admin_headers),
        await test_client.get("/api/dashboard/conversations?offset=-1", headers=admin_headers),
    ]

    assert [response.status_code for response in responses] == [422, 422, 422]


async def test_agent_stats_http_keeps_top_performers_wire_contract(test_client, admin_headers):
    """智能体统计 HTTP 契约保留概览字段与既有 TOP 5 数据。"""
    response = await test_client.get("/api/dashboard/stats/agents", headers=admin_headers)

    assert response.status_code == 200, response.text
    assert set(response.json()) == {
        "total_agents",
        "agent_conversation_counts",
        "agent_satisfaction_rates",
        "agent_tool_usage",
        "top_performing_agents",
        "agent_names",
    }


async def test_admin_can_fetch_stats(test_client, admin_headers):
    """Test that the timeseries stats endpoint returns consistent values."""
    response = await test_client.get(
        "/api/dashboard/stats/calls/timeseries?type=models&time_range=14days",
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total_count"] >= 0
    assert len(data["data"]) == 14
    assert isinstance(data["categories"], list)


async def test_knowledge_stats_matches_runtime_capability(test_client, admin_headers):
    response = await test_client.get("/api/dashboard/stats/knowledge", headers=admin_headers)

    if not knowledge_capability_enabled():
        assert response.status_code == 404, response.text
        return

    assert response.status_code == 200, response.text
    assert set(response.json()) == {
        "total_databases",
        "total_files",
        "total_nodes",
        "total_storage_size",
        "databases_by_type",
        "file_type_distribution",
    }


async def test_admin_can_fetch_thread_analytics(test_client, admin_headers):
    """Test that thread analytics endpoint returns complete statistics schema."""
    response = await test_client.get(
        "/api/dashboard/stats/threads?time_range=30days",
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert "summary" in data
    assert "daily_trends" in data
    assert "depth_distribution" in data
    assert "agent_distribution" in data
    assert "top_users" in data
    assert "status_distribution" in data
    assert len(data["daily_trends"]) == 30
    assert data["summary"]["total_threads"] >= 0


async def test_dashboard_http_applies_subagent_and_deleted_conversation_scopes(test_client, admin_headers):
    default_agent = await test_client.get("/api/agent/default", headers=admin_headers)
    assert default_agent.status_code == 200, default_agent.text
    agent = default_agent.json()["agent"]
    agent_id = str(agent.get("slug") or agent["agent_id"])
    marker = f"dashboard-scope-{uuid.uuid4().hex[:10]}"

    async def analytics(*, include_subagents: bool) -> dict:
        response = await test_client.get(
            "/api/dashboard/stats/threads",
            params={
                "time_range": "30days",
                "agent_id": agent_id,
                "include_subagents": str(include_subagents).lower(),
            },
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        return response.json()

    baseline_default = await analytics(include_subagents=False)
    baseline_including_subagents = await analytics(include_subagents=True)
    thread_ids = []
    for status in ("active", "subagent", "deleted"):
        response = await test_client.post(
            "/api/chat/thread",
            headers=admin_headers,
            json={
                "agent_id": agent_id,
                "title": make_test_conversation_title(f"{marker}-{status}"),
                "metadata": make_test_conversation_metadata(marker),
            },
        )
        assert response.status_code == 200, response.text
        thread_ids.append(str(response.json().get("thread_id") or response.json()["id"]))

    await _set_conversation_statuses(thread_ids[1], thread_ids[2])

    default_scope = await analytics(include_subagents=False)
    subagent_scope = await analytics(include_subagents=True)
    assert default_scope["summary"]["total_threads"] == baseline_default["summary"]["total_threads"] + 1
    assert subagent_scope["summary"]["total_threads"] == baseline_including_subagents["summary"]["total_threads"] + 2

    default_audit = await test_client.get(
        "/api/dashboard/conversations",
        params={"search": marker, "limit": 10},
        headers=admin_headers,
    )
    deleted_audit = await test_client.get(
        "/api/dashboard/conversations",
        params={"search": marker, "status": "deleted", "limit": 10},
        headers=admin_headers,
    )
    assert default_audit.status_code == 200, default_audit.text
    assert deleted_audit.status_code == 200, deleted_audit.text
    assert {item["thread_id"] for item in default_audit.json()["items"]} == set(thread_ids[:2])
    assert {item["thread_id"] for item in deleted_audit.json()["items"]} == {thread_ids[2]}


async def test_admin_can_fetch_feedbacks(test_client, admin_headers):
    """Test that feedback endpoint returns 200 and handles the User join correctly."""
    response = await test_client.get("/api/dashboard/feedbacks", headers=admin_headers)
    assert response.status_code == 200, f"feedbacks failed: {response.text}"
    assert isinstance(response.json(), list)


async def test_current_organization_stats_follow_dashboard_management_scope(test_client, dashboard_scope_users):
    a_response = await test_client.get(
        "/api/dashboard/stats/current-organization",
        headers=dashboard_scope_users["a"],
    )
    b_response = await test_client.get(
        "/api/dashboard/stats/current-organization",
        headers=dashboard_scope_users["b"],
    )
    child_response = await test_client.get(
        f"/api/dashboard/stats/current-organization?department_id={dashboard_scope_users['department_child']}",
        headers=dashboard_scope_users["a"],
    )
    hidden_response = await test_client.get(
        f"/api/dashboard/stats/current-organization?department_id={dashboard_scope_users['department_b']}",
        headers=dashboard_scope_users["a"],
    )
    forbidden_response = await test_client.get(
        "/api/dashboard/stats/current-organization",
        headers=dashboard_scope_users["without_permission"],
    )

    assert a_response.status_code == 200, a_response.text
    assert b_response.status_code == 200, b_response.text
    assert child_response.status_code == 200, child_response.text
    assert a_response.json()["total_users"] == 2
    assert a_response.json()["total_departments"] == 2
    assert b_response.json()["total_users"] == 1
    assert b_response.json()["total_departments"] == 1
    assert child_response.json()["total_users"] == 1
    assert hidden_response.status_code == 404
    assert forbidden_response.status_code == 403


async def test_historical_stats_keep_write_time_organization_and_mark_inferred_data(
    test_client,
    dashboard_scope_users,
):
    async with pg_manager.get_async_session_context() as session:
        repository = ConversationRepository(session)
        project_id = str(uuid.uuid4())
        session.add(
            Project(
                id=project_id,
                uid=dashboard_scope_users["child_user_uid"],
                selection_status="implicit",
                workdir_path=f"projects/{project_id}",
                directory_mode="managed",
            )
        )
        await session.flush()
        before_move = await repository.add_conversation(
            uid=dashboard_scope_users["child_user_uid"],
            agent_id="pytest-dashboard-agent",
            thread_id=f"pytest-dashboard-before-{uuid.uuid4().hex}",
            project_id=project_id,
        )
        before_message = Message(conversation=before_move, role="assistant", content="before")
        session.add(before_message)
        await session.flush()
        await repository.add_tool_call(before_message.id, "before_move", status="success")

        feedback_response = await test_client.post(
            f"/api/chat/message/{before_message.id}/feedback",
            json={"rating": "like", "reason": None},
            headers=dashboard_scope_users["without_permission"],
        )
        assert feedback_response.status_code == 200, feedback_response.text

        child_user = await session.get(User, dashboard_scope_users["child_user_id"])
        child_user.department_id = dashboard_scope_users["department_b"]
        await session.commit()

        after_move = await repository.add_conversation(
            uid=dashboard_scope_users["child_user_uid"],
            agent_id="pytest-dashboard-agent",
            thread_id=f"pytest-dashboard-after-{uuid.uuid4().hex}",
            project_id=project_id,
        )
        after_message = Message(conversation=after_move, role="assistant", content="after")
        session.add(after_message)
        await session.flush()
        await repository.add_tool_call(after_message.id, "after_move", status="success")
        session.add(
            ToolCall(
                message=after_message,
                tool_name="legacy_inferred",
                status="success",
                organization_id_snapshot=dashboard_scope_users["department_b"],
                organization_path_snapshot=dashboard_scope_users["department_b_path"],
                organization_snapshot_inferred=True,
            )
        )
        await session.commit()
        await session.refresh(before_move)
        feedback = await session.scalar(select(MessageFeedback).where(MessageFeedback.message_id == before_message.id))
        login_log = await session.scalar(
            select(OperationLog)
            .where(OperationLog.user_id == dashboard_scope_users["manager_a_id"])
            .order_by(OperationLog.id.desc())
        )
        assert before_move.organization_path_snapshot == dashboard_scope_users["department_child_path"]
        assert feedback.organization_path_snapshot == dashboard_scope_users["department_child_path"]
        assert feedback.organization_snapshot_inferred is False
        assert login_log.organization_snapshot_inferred is False

    a_tools = await test_client.get("/api/dashboard/stats/tools", headers=dashboard_scope_users["a"])
    b_tools = await test_client.get("/api/dashboard/stats/tools", headers=dashboard_scope_users["b"])
    a_current = await test_client.get(
        "/api/dashboard/stats/current-organization",
        headers=dashboard_scope_users["a"],
    )
    b_current = await test_client.get(
        "/api/dashboard/stats/current-organization",
        headers=dashboard_scope_users["b"],
    )
    b_basic = await test_client.get("/api/dashboard/stats", headers=dashboard_scope_users["b"])
    a_feedback = await test_client.get("/api/dashboard/feedbacks", headers=dashboard_scope_users["a"])
    b_feedback = await test_client.get("/api/dashboard/feedbacks", headers=dashboard_scope_users["b"])
    a_timeseries = await test_client.get(
        "/api/dashboard/stats/calls/timeseries?type=tools&time_range=14days",
        headers=dashboard_scope_users["a"],
    )
    b_timeseries = await test_client.get(
        "/api/dashboard/stats/calls/timeseries?type=tools&time_range=14days",
        headers=dashboard_scope_users["b"],
    )
    hidden_history = await test_client.get(
        f"/api/dashboard/stats/tools?department_id={dashboard_scope_users['department_b']}",
        headers=dashboard_scope_users["a"],
    )

    assert a_tools.status_code == 200, a_tools.text
    assert b_tools.status_code == 200, b_tools.text
    assert a_tools.json()["total_calls"] == 1
    assert b_tools.json()["total_calls"] == 2
    assert a_current.json()["total_users"] == 1
    assert b_current.json()["total_users"] == 2
    assert b_basic.json()["contains_inferred_data"] is True
    assert len(a_feedback.json()) == 1
    assert b_feedback.json() == []
    assert a_timeseries.json()["total_count"] == 1
    assert b_timeseries.json()["total_count"] == 2
    assert hidden_history.status_code == 404


async def test_resource_stats_separate_creation_snapshot_from_shared_visibility(
    test_client,
    dashboard_scope_users,
):
    """资源创建归属保持不变，共享可见随共享范围和当前人员关系计算。"""

    a_before = (await test_client.get("/api/dashboard/stats/resources", headers=dashboard_scope_users["a"])).json()
    b_before = (await test_client.get("/api/dashboard/stats/resources", headers=dashboard_scope_users["b"])).json()

    shared_to_b = {
        "version": 2,
        "read_scope": {
            "access_level": "department",
            "department_ids": [dashboard_scope_users["department_b"]],
            "user_uids": [],
        },
        "manage_scope": None,
    }
    global_share = {
        "version": 2,
        "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
        "manage_scope": None,
    }
    suffix = uuid.uuid4().hex[:10]
    kb = await KnowledgeBaseRepository().create(
        {
            "kb_id": f"kb_pytest_{suffix}",
            "name": f"pytest-kb-{suffix}",
            "kb_type": "milvus",
            "share_config": shared_to_b,
            "created_by": dashboard_scope_users["child_user_uid"],
        }
    )

    async with pg_manager.get_async_session_context() as session:
        agent = await AgentRepository(session).create(
            name=f"pytest-agent-{suffix}",
            backend_id="ChatbotAgent",
            slug=f"pytest-agent-{suffix}",
            share_config=shared_to_b,
            created_by=dashboard_scope_users["child_user_uid"],
        )
        skill = await SkillRepository(session).create(
            slug=f"pytest-skill-{suffix}",
            name=f"pytest-skill-{suffix}",
            description="Dashboard 资源统计集成测试",
            source_type="upload",
            tool_dependencies=[],
            mcp_dependencies=[],
            skill_dependencies=[],
            dir_path=f"skills/pytest-skill-{suffix}",
            share_config=global_share,
            created_by=dashboard_scope_users["child_user_uid"],
        )
        inferred_kb = KnowledgeBase(
            kb_id=f"kb_pytest_inferred_{suffix}",
            name=f"pytest-kb-inferred-{suffix}",
            kb_type="milvus",
            share_config=global_share,
            created_by=dashboard_scope_users["child_user_uid"],
            organization_id_snapshot=dashboard_scope_users["department_child"],
            organization_path_snapshot=dashboard_scope_users["department_child_path"],
            organization_snapshot_inferred=True,
        )
        session.add(inferred_kb)
        await session.commit()

        child_user = await session.get(User, dashboard_scope_users["child_user_id"])
        child_user.department_id = dashboard_scope_users["department_b"]
        await session.commit()
        await session.refresh(agent)
        await session.refresh(skill)

        assert kb.organization_path_snapshot == dashboard_scope_users["department_child_path"]
        assert agent.organization_path_snapshot == dashboard_scope_users["department_child_path"]
        assert skill.organization_path_snapshot == dashboard_scope_users["department_child_path"]
        assert agent.organization_snapshot_inferred is False
        assert skill.organization_snapshot_inferred is False

    a_after_response = await test_client.get("/api/dashboard/stats/resources", headers=dashboard_scope_users["a"])
    b_after_response = await test_client.get("/api/dashboard/stats/resources", headers=dashboard_scope_users["b"])
    hidden_response = await test_client.get(
        f"/api/dashboard/stats/resources?department_id={dashboard_scope_users['department_b']}",
        headers=dashboard_scope_users["a"],
    )
    a_knowledge = await test_client.get("/api/dashboard/stats/knowledge", headers=dashboard_scope_users["a"])
    b_knowledge = await test_client.get("/api/dashboard/stats/knowledge", headers=dashboard_scope_users["b"])

    assert a_after_response.status_code == 200, a_after_response.text
    assert b_after_response.status_code == 200, b_after_response.text
    assert hidden_response.status_code == 404
    assert a_knowledge.status_code == 200, a_knowledge.text
    assert b_knowledge.status_code == 200, b_knowledge.text
    a_after = a_after_response.json()
    b_after = b_after_response.json()

    assert a_after["knowledge_bases"]["creation_count"] == a_before["knowledge_bases"]["creation_count"] + 2
    assert a_after["agents"]["creation_count"] == a_before["agents"]["creation_count"] + 1
    assert a_after["skills"]["creation_count"] == a_before["skills"]["creation_count"] + 1
    assert b_after["knowledge_bases"]["creation_count"] == b_before["knowledge_bases"]["creation_count"]
    assert b_after["agents"]["creation_count"] == b_before["agents"]["creation_count"]
    assert b_after["skills"]["creation_count"] == b_before["skills"]["creation_count"]

    assert a_after["knowledge_bases"]["shared_visible_count"] == a_before["knowledge_bases"]["shared_visible_count"] + 1
    assert a_after["agents"]["shared_visible_count"] == a_before["agents"]["shared_visible_count"]
    assert a_after["skills"]["shared_visible_count"] == a_before["skills"]["shared_visible_count"] + 1
    assert b_after["knowledge_bases"]["shared_visible_count"] == b_before["knowledge_bases"]["shared_visible_count"] + 2
    assert b_after["agents"]["shared_visible_count"] == b_before["agents"]["shared_visible_count"] + 1
    assert b_after["skills"]["shared_visible_count"] == b_before["skills"]["shared_visible_count"] + 1
    assert a_after["knowledge_bases"]["contains_inferred_data"] is True
    assert a_knowledge.json()["total_databases"] == a_before["knowledge_bases"]["shared_visible_count"] + 1
    assert b_knowledge.json()["total_databases"] == b_before["knowledge_bases"]["shared_visible_count"] + 2
