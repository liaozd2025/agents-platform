"""Unit tests for DashboardService and Thread analytics."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import yuxi.services.dashboard_service as dashboard_service
from yuxi.repositories.dashboard_repository import DashboardRepository
from yuxi.services.dashboard_scope_service import dashboard_tool_history_filter
from yuxi.services.dashboard_service import DashboardService
from yuxi.storage.postgres.models_business import (
    Agent,
    AgentRun,
    Base,
    Conversation,
    ConversationStats,
    Department,
    Message,
    MessageFeedback,
    ToolCall,
    User,
)
from yuxi.utils.datetime_utils import utc_now_naive

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture()
async def dashboard_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        dept = Department(name="Engineering")
        superadmin = User(
            username="Super Admin",
            uid="uid-superadmin",
            password_hash="$argon2id$placeholder",
            department=dept,
        )
        user1 = User(
            username="Alice",
            uid="uid-alice",
            password_hash="$argon2id$placeholder",
            department=dept,
        )
        user2 = User(
            username="Bob",
            uid="uid-bob",
            password_hash="$argon2id$placeholder",
            department=dept,
        )
        deleted_user = User(
            username="Deleted User",
            uid="uid-deleted",
            password_hash="$argon2id$placeholder",
            department=dept,
            is_deleted=1,
        )

        agent1 = Agent(
            slug="agent-helper",
            backend_id="b-1",
            name="Helper Agent",
            share_config={},
        )
        agent2 = Agent(
            slug="agent-coder",
            backend_id="b-2",
            name="Coder Agent",
            share_config={},
        )

        now = utc_now_naive()
        yesterday = now - timedelta(days=1)

        conv1 = Conversation(
            thread_id="thread-101",
            project_id="p-1",
            uid="uid-alice",
            agent_id="agent-helper",
            title="Alice Helper Query",
            status="active",
            is_pinned=True,
            created_at=yesterday,
            updated_at=now,
        )
        conv2 = Conversation(
            thread_id="thread-102",
            project_id="p-2",
            uid="uid-bob",
            agent_id="agent-coder",
            title="Bob Coder Task",
            status="active",
            is_pinned=False,
            created_at=now,
            updated_at=now,
        )
        conv3 = Conversation(
            thread_id="thread-103",
            project_id="p-3",
            uid="uid-alice",
            agent_id="agent-coder",
            title="Alice Python Debug",
            status="archived",
            is_pinned=False,
            created_at=yesterday,
            updated_at=yesterday,
        )
        deleted_conversation = Conversation(
            thread_id="thread-deleted",
            project_id="p-deleted",
            uid="uid-alice",
            agent_id="agent-helper",
            title="Deleted conversation",
            status="deleted",
            created_at=yesterday,
            updated_at=now,
        )
        deleted_user_conversation = Conversation(
            thread_id="thread-deleted-user",
            project_id="p-deleted-user",
            uid="uid-deleted",
            agent_id="agent-helper",
            title="Deleted user conversation",
            status="active",
            created_at=yesterday,
            updated_at=now,
        )
        missing_agent_conversation = Conversation(
            thread_id="thread-missing-agent",
            project_id="p-missing-agent",
            uid="uid-alice",
            agent_id="removed-agent",
            title="Removed agent conversation",
            status="active",
            created_at=yesterday,
            updated_at=now,
        )
        subagent_conversation = Conversation(
            thread_id="thread-subagent",
            project_id="p-subagent",
            uid="uid-alice",
            agent_id="agent-helper",
            title="Subagent conversation",
            status="subagent",
            created_at=yesterday,
            updated_at=now,
        )

        stats1 = ConversationStats(conversation=conv1, message_count=4, total_tokens=1200)
        stats2 = ConversationStats(conversation=conv2, message_count=8, total_tokens=3500)
        stats3 = ConversationStats(conversation=conv3, message_count=1, total_tokens=300)

        msg1 = Message(conversation=conv1, role="user", content="Hello", created_at=yesterday)
        msg2 = Message(conversation=conv1, role="assistant", content="Hi there!", created_at=yesterday)
        msg3 = Message(conversation=conv2, role="user", content="Write code", created_at=now)
        msg4 = Message(conversation=conv2, role="assistant", content="Here is code", created_at=now)
        removed_agent_message = Message(
            conversation=missing_agent_conversation,
            role="assistant",
            content="Historical removed agent output",
            created_at=now,
        )

        tool1 = ToolCall(message=msg4, tool_name="bash", status="success", created_at=now)
        removed_agent_tool = ToolCall(
            message=removed_agent_message,
            tool_name="legacy_tool",
            status="success",
            created_at=now,
        )

        feedback1 = MessageFeedback(message=msg2, uid="uid-alice", rating="like", created_at=yesterday)
        removed_agent_feedback = MessageFeedback(
            message=removed_agent_message,
            uid="uid-alice",
            rating="dislike",
            created_at=now,
        )
        older_run = AgentRun(
            id="run-thread-102-older",
            conversation_thread_id=conv2.thread_id,
            runtime_scope_id=conv2.thread_id,
            agent_slug=conv2.agent_id,
            uid=conv2.uid,
            status="failed",
            request_id="request-thread-102-older",
            conversation_id=conv2.id,
            run_type="chat",
            input_payload={},
            created_at=yesterday,
            finished_at=yesterday,
        )
        completed_run = AgentRun(
            id="run-thread-102-completed",
            conversation_thread_id=conv2.thread_id,
            runtime_scope_id=conv2.thread_id,
            agent_slug=conv2.agent_id,
            uid=conv2.uid,
            status="completed",
            request_id="request-thread-102-completed",
            conversation_id=conv2.id,
            run_type="chat",
            input_payload={},
            created_at=now,
            finished_at=now,
        )

        db.add_all(
            [
                dept,
                superadmin,
                user1,
                user2,
                deleted_user,
                agent1,
                agent2,
                conv1,
                conv2,
                conv3,
                deleted_conversation,
                deleted_user_conversation,
                missing_agent_conversation,
                subagent_conversation,
                stats1,
                stats2,
                stats3,
                msg1,
                msg2,
                msg3,
                msg4,
                removed_agent_message,
                tool1,
                removed_agent_tool,
                feedback1,
                removed_agent_feedback,
                older_run,
                completed_run,
            ]
        )
        await db.commit()
        yield db
    await engine.dispose()


async def test_dashboard_service_basic_stats(dashboard_db):
    service = DashboardService(dashboard_db)
    stats = await service.get_basic_stats()

    assert stats["total_conversations"] == 5
    assert stats["active_conversations"] == 4
    assert stats["total_messages"] == 5
    assert stats["total_users"] == 3
    assert stats["feedback_stats"]["total_feedbacks"] == 2
    assert stats["feedback_stats"]["satisfaction_rate"] == 50.0

    tool_stats = await service.get_tool_call_stats()
    assert tool_stats["total_calls"] == 2

    user_stats = await service.get_user_activity_stats()
    assert len(user_stats["daily_active_users"]) == 120
    assert user_stats["daily_active_users"][0]["date"] < user_stats["daily_active_users"][-1]["date"]

    feedbacks = await service.get_feedbacks()
    assert len(feedbacks) == 2


async def test_dashboard_self_scope_uses_tool_call_conversation_owner(dashboard_db):
    authorization = SimpleNamespace(
        user=SimpleNamespace(id=3, uid="uid-bob", department_id=None),
        permission_scopes=lambda _permission: (("self", frozenset()),),
    )
    scope_filter = await dashboard_tool_history_filter(dashboard_db, authorization, None)
    repository = DashboardRepository(dashboard_db)

    tool_stats = await repository.get_tool_call_stats(scope_filter=scope_filter)
    assert tool_stats["total_calls"] == 1
    assert tool_stats["most_used_tools"] == [{"tool_name": "bash", "count": 1}]

    await dashboard_db.execute(
        update(ToolCall).where(ToolCall.tool_name == "legacy_tool").values(organization_snapshot_inferred=True)
    )
    await dashboard_db.commit()
    basic_stats = await repository.get_basic_stats(tool_filter=scope_filter)
    assert basic_stats["contains_inferred_data"] is False


async def test_resource_scope_aggregation_lives_in_dashboard_service(dashboard_db, monkeypatch):
    service = DashboardService(dashboard_db)
    owned = SimpleNamespace(
        created_by="uid-bob",
        organization_path_snapshot="/1/2/",
        organization_snapshot_inferred=True,
        share_config={"version": 2, "read_scope": None, "manage_scope": None},
        source_scope="shared",
    )
    authorization = SimpleNamespace(
        user=SimpleNamespace(id=3, uid="uid-bob"),
        allows=lambda _permission, target: target.owner_user_id == 3,
    )

    async def list_resources(_model):
        return [owned]

    async def subjects(_db, _authorization, _department_id):
        return [{"uid": "uid-bob", "department_ancestor_ids": (1, 2)}]

    monkeypatch.setattr(service.repo, "list_resources", list_resources)
    monkeypatch.setattr(dashboard_service, "dashboard_resource_subjects", subjects)
    monkeypatch.setattr(dashboard_service, "knowledge_capability_enabled", lambda: False)

    result = await service.get_resource_scope_stats(authorization=authorization, department_id=2)

    assert result["knowledge_bases"]["creation_count"] == 0
    assert (
        result["agents"]
        == result["skills"]
        == {
            "creation_count": 1,
            "shared_visible_count": 1,
            "contains_inferred_data": True,
        }
    )
    assert result["contains_inferred_data"] is True


async def test_agent_analytics_keeps_top_performers_wire_contract(dashboard_db):
    """智能体统计保留历史 Agent 概览与既有 TOP 5 协议。"""
    analytics = await DashboardService(dashboard_db).get_agent_analytics()

    assert set(analytics) == {
        "total_agents",
        "agent_conversation_counts",
        "agent_satisfaction_rates",
        "agent_tool_usage",
        "top_performing_agents",
        "agent_names",
    }
    assert analytics["total_agents"] == 3
    assert analytics["agent_names"] == {
        "agent-helper": "Helper Agent",
        "agent-coder": "Coder Agent",
        "removed-agent": "removed-agent",
    }
    assert {item["agent_id"] for item in analytics["top_performing_agents"]} == {
        "agent-helper",
        "agent-coder",
        "removed-agent",
    }


async def test_dashboard_service_thread_analytics(dashboard_db):
    service = DashboardService(dashboard_db)
    analytics = await service.get_thread_analytics(time_range="7days")

    summary = analytics["summary"]
    assert summary["total_threads"] == 5
    assert summary["active_threads"] >= 1
    assert summary["pinned_threads"] == 1
    assert summary["total_messages"] == 5
    assert summary["total_tokens"] == 5000
    assert summary["avg_messages_per_thread"] > 0
    assert summary["avg_tokens_per_thread"] > 0

    assert len(analytics["daily_trends"]) == 7

    depth = analytics["depth_distribution"]
    assert depth["0 条"] == 2  # deleted user and removed agent histories have no ConversationStats
    assert depth["1-2 条"] == 1  # conv3 has 1 message
    assert depth["3-5 条"] == 1  # conv1 has 4 messages
    assert depth["6-10 条"] == 1  # conv2 has 8 messages

    agents = analytics["agent_distribution"]
    assert len(agents) == 3
    coder_stat = next(a for a in agents if a["agent_id"] == "agent-coder")
    assert coder_stat["thread_count"] == 2
    assert coder_stat["agent_name"] == "Coder Agent"
    removed_stat = next(a for a in agents if a["agent_id"] == "removed-agent")
    assert removed_stat["thread_count"] == 1
    assert removed_stat["agent_name"] == "removed-agent"

    with_subagents = await service.get_thread_analytics(time_range="7days", include_subagents=True)
    assert with_subagents["summary"]["total_threads"] == 6
    helper_with_subagent = next(
        item for item in with_subagents["agent_distribution"] if item["agent_id"] == "agent-helper"
    )
    assert helper_with_subagent["thread_count"] == 3

    top_users = analytics["top_users"]
    assert len(top_users) >= 2
    alice_stat = next(u for u in top_users if u["uid"] == "uid-alice")
    assert alice_stat["username"] == "Alice"
    assert alice_stat["thread_count"] == 3
    assert next(u for u in top_users if u["uid"] == "uid-deleted")["username"] == "Deleted User"

    assert analytics["status_distribution"]["active"] == 4
    assert analytics["status_distribution"]["archived"] == 1

    coder_only = await service.get_thread_analytics(time_range="7days", agent_id="agent-coder")
    assert coder_only["summary"]["total_threads"] == 2
    assert coder_only["summary"]["total_messages"] == 2
    assert [item["agent_id"] for item in coder_only["agent_distribution"]] == ["agent-coder"]
    assert coder_only["status_distribution"] == {"active": 1, "archived": 1}
    assert {item["uid"] for item in coder_only["top_users"]} == {"uid-alice", "uid-bob"}


async def test_thread_analytics_groups_daily_trends_by_shanghai_date(dashboard_db):
    service = DashboardService(dashboard_db)
    fixed_now = datetime(2026, 8, 24, 1, 0)
    baseline = await service.repo.get_thread_analytics(time_range="7days", now=fixed_now)
    baseline_by_date = {item["date"]: item for item in baseline["daily_trends"]}

    boundary_conversation = Conversation(
        thread_id="thread-shanghai-boundary",
        project_id="p-boundary",
        uid="uid-alice",
        agent_id="agent-helper",
        title="Shanghai boundary",
        status="active",
        created_at=datetime(2026, 8, 23, 16, 30),
        updated_at=datetime(2026, 8, 23, 16, 30),
    )
    boundary_message = Message(
        conversation=boundary_conversation,
        role="user",
        content="After Shanghai midnight",
        created_at=datetime(2026, 8, 23, 16, 30),
    )
    dashboard_db.add_all([boundary_conversation, boundary_message])
    await dashboard_db.commit()

    analytics = await service.repo.get_thread_analytics(time_range="7days", now=fixed_now)
    trend_by_date = {item["date"]: item for item in analytics["daily_trends"]}

    assert trend_by_date["2026-08-24"]["new_threads"] == baseline_by_date["2026-08-24"]["new_threads"] + 1
    assert trend_by_date["2026-08-24"]["active_threads"] == baseline_by_date["2026-08-24"]["active_threads"] + 1
    assert trend_by_date["2026-08-24"]["message_count"] == baseline_by_date["2026-08-24"]["message_count"] + 1


async def test_thread_analytics_query_count_does_not_grow_with_time_range(dashboard_db):
    service = DashboardService(dashboard_db)
    engine = dashboard_db.bind.sync_engine
    statement_counts = []
    current_count = 0

    def count_statement(*_args):
        nonlocal current_count
        current_count += 1

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        await service.get_thread_analytics(time_range="7days")
        statement_counts.append(current_count)
        current_count = 0
        await service.get_thread_analytics(time_range="90days")
        statement_counts.append(current_count)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert statement_counts[0] == statement_counts[1]
    assert statement_counts[0] <= 12


async def test_dashboard_service_list_conversations_search(dashboard_db):
    service = DashboardService(dashboard_db)

    all_convs = await service.list_conversations(limit=20)
    assert all_convs["total"] == 6
    assert len(all_convs["items"]) == 6
    assert all(item["status"] != "deleted" for item in all_convs["items"])
    deleted_convs = await service.list_conversations(status="deleted", limit=20)
    assert deleted_convs["total"] == 1
    assert deleted_convs["items"][0]["thread_id"] == "thread-deleted"
    deleted_item = next(item for item in all_convs["items"] if item["thread_id"] == "thread-deleted-user")
    missing_agent_item = next(item for item in all_convs["items"] if item["thread_id"] == "thread-missing-agent")
    assert deleted_item["user_deleted"] is True
    assert missing_agent_item["agent_deleted"] is True

    search_result = await service.list_conversations(search="Python")
    assert search_result["total"] == 1
    assert search_result["items"][0]["thread_id"] == "thread-103"
    assert search_result["items"][0]["username"] == "Alice"
    assert search_result["items"][0]["agent_name"] == "Coder Agent"

    completed_item = next(item for item in all_convs["items"] if item["thread_id"] == "thread-102")
    assert completed_item["status"] == "active"
    assert completed_item["run_status"] == "completed"
    no_run_item = next(item for item in all_convs["items"] if item["thread_id"] == "thread-101")
    assert no_run_item["run_status"] is None

    active_only = await service.list_conversations(status="active")
    assert active_only["total"] == 4
    assert len(active_only["items"]) == 4

    options = await service.get_conversation_filter_options()
    assert next(item for item in options["users"] if item["uid"] == "uid-deleted")["is_deleted"] is True
    assert next(item for item in options["agents"] if item["agent_id"] == "removed-agent")["is_deleted"] is True


async def test_dashboard_service_conversation_detail(dashboard_db):
    service = DashboardService(dashboard_db)
    detail = await service.get_conversation_detail("thread-102")

    assert detail is not None
    assert detail["thread_id"] == "thread-102"
    assert detail["total_tokens"] == 3500
    assert detail["user_deleted"] is False
    assert detail["agent_deleted"] is False
    assert detail["status"] == "active"
    assert detail["run_status"] == "completed"
    assert len(detail["messages"]) == 2
    assistant_msg = next(m for m in detail["messages"] if m["role"] == "assistant")
    assert "tool_calls" in assistant_msg
    assert assistant_msg["tool_calls"][0]["tool_name"] == "bash"
