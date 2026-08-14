"""
Integration tests for dashboard router endpoints.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import (
    ROOT_DEPARTMENT_ID,
    Department,
    Conversation,
    ConversationStats,
    Message,
    MessageFeedback,
    OperationLog,
    Role,
    RolePermission,
    ToolCall,
    User,
    UserRoleAssignment,
)
from yuxi.utils.auth_utils import AuthUtils

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
                role="user",
                department=department_a,
            ),
            User(
                username=f"Dashboard B {suffix}",
                uid=f"pytest_dashboard_b_{suffix}",
                password_hash=AuthUtils.hash_password(password),
                role="user",
                department=department_b,
            ),
            User(
                username=f"Dashboard Child {suffix}",
                uid=f"pytest_dashboard_child_{suffix}",
                password_hash=AuthUtils.hash_password(password),
                role="user",
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
            "child_user_id": user_ids[2],
            "child_user_uid": user_uids[2],
        }
    finally:
        async with pg_manager.get_async_session_context() as session:
            conversation_ids = list(
                await session.scalars(select(Conversation.id).where(Conversation.uid.in_(user_uids)))
            )
            if conversation_ids:
                message_ids = list(
                    await session.scalars(
                        select(Message.id).where(Message.conversation_id.in_(conversation_ids))
                    )
                )
                if message_ids:
                    await session.execute(delete(ToolCall).where(ToolCall.message_id.in_(message_ids)))
                    await session.execute(
                        delete(MessageFeedback).where(MessageFeedback.message_id.in_(message_ids))
                    )
                    await session.execute(delete(Message).where(Message.id.in_(message_ids)))
                await session.execute(
                    delete(ConversationStats).where(ConversationStats.conversation_id.in_(conversation_ids))
                )
                await session.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
            await session.execute(delete(OperationLog).where(OperationLog.user_id.in_(user_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
            await session.execute(delete(Role).where(Role.id == role_id))
            for department_id in department_ids:
                await session.execute(delete(Department).where(Department.id == department_id))
        await pg_manager.async_engine.dispose()


async def test_dashboard_requires_authentication(test_client):
    response = await test_client.get("/api/dashboard/conversations")
    assert response.status_code == 401


async def test_standard_user_is_forbidden(test_client, standard_user):
    response = await test_client.get("/api/dashboard/conversations", headers=standard_user["headers"])
    assert response.status_code == 403


async def test_admin_can_fetch_conversations(test_client, admin_headers):
    response = await test_client.get("/api/dashboard/conversations", headers=admin_headers)
    assert response.status_code == 200, response.text
    assert isinstance(response.json(), list)


async def test_admin_can_fetch_stats(test_client, admin_headers):
    """Test that all stats endpoints return 200 and don't crash on DB queries."""

    # Test call timeseries stats for all types
    types = ["models", "agents", "tokens", "tools"]
    for stats_type in types:
        response = await test_client.get(
            f"/api/dashboard/stats/calls/timeseries?type={stats_type}&time_range=14days", headers=admin_headers
        )
        assert response.status_code == 200, f"{stats_type} stats failed: {response.text}"
        data = response.json()
        assert "data" in data
        assert "categories" in data

    # Test user activity stats
    response = await test_client.get("/api/dashboard/stats/users", headers=admin_headers)
    assert response.status_code == 200, f"user stats failed: {response.text}"
    assert "total_users" in response.json()

    # Test tool call stats
    response = await test_client.get("/api/dashboard/stats/tools", headers=admin_headers)
    assert response.status_code == 200, f"tool stats failed: {response.text}"
    assert "total_calls" in response.json()


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
        before_move = await repository.add_conversation(
            uid=dashboard_scope_users["child_user_uid"],
            agent_id="pytest-dashboard-agent",
            thread_id=f"pytest-dashboard-before-{uuid.uuid4().hex}",
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
        feedback = await session.scalar(
            select(MessageFeedback).where(MessageFeedback.message_id == before_message.id)
        )
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
