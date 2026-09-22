from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from yuxi.services.conversation_service import get_thread_history_view
from yuxi.storage.postgres.models_business import AgentRun, Base, Conversation, Message, Project, ToolCall

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(
            Project(
                id="project-thread-1",
                uid="user-1",
                selection_status="implicit",
                directory_mode="managed",
                workdir_path="projects/project-thread-1",
            )
        )
        await db.commit()
        yield db
    await engine.dispose()


async def test_queue_history_keeps_each_request_with_its_reply(session):
    started_at = datetime(2026, 7, 12, 9, 0, 0)
    session.add(
        Conversation(
            id=1,
            thread_id="thread-1",
            project_id="project-thread-1",
            uid="user-1",
            agent_id="main",
            status="active",
        )
    )
    session.add(
        AgentRun(
            id="run-a",
            conversation_thread_id="thread-1",
            runtime_scope_id="thread-1",
            agent_slug="main",
            uid="user-1",
            request_id="request-a",
            conversation_id=1,
            input_payload={},
            status="completed",
            created_at=started_at,
        )
    )
    session.add_all(
        [
            Message(
                id=1,
                conversation_id=1,
                role="user",
                content="A",
                request_id="request-a",
                run_id="run-a",
                delivery_status="complete",
                created_at=started_at,
            ),
            Message(
                id=2,
                conversation_id=1,
                role="user",
                content="B",
                request_id="request-b",
                delivery_status="queued",
                created_at=started_at + timedelta(seconds=1),
            ),
            Message(
                id=3,
                conversation_id=1,
                role="assistant",
                content="A reply",
                extra_metadata={"additional_kwargs": {"reasoning_content": "A reasoning"}},
                run_id="run-a",
                delivery_status="complete",
                created_at=started_at + timedelta(seconds=2),
            ),
        ]
    )
    await session.commit()

    queued_history = await get_thread_history_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=session,
    )
    assert [message["content"] for message in queued_history["history"]] == ["A", "A reply"]
    assert [message.get("reasoning_content", "") for message in queued_history["history"]] == ["", "A reasoning"]

    request_b = await session.get(Message, 2)
    request_b.run_id = "run-b"
    request_b.delivery_status = "complete"
    session.add(
        AgentRun(
            id="run-b",
            conversation_thread_id="thread-1",
            runtime_scope_id="thread-1",
            agent_slug="main",
            uid="user-1",
            request_id="request-b",
            conversation_id=1,
            input_payload={},
            status="completed",
            created_at=started_at + timedelta(seconds=3),
        )
    )
    session.add(
        Message(
            id=4,
            conversation_id=1,
            role="assistant",
            content="B reply",
            run_id="run-b",
            delivery_status="complete",
            created_at=started_at + timedelta(seconds=4),
        )
    )
    await session.commit()

    completed_history = await get_thread_history_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=session,
    )
    assert [message["content"] for message in completed_history["history"]] == [
        "A",
        "A reply",
        "B",
        "B reply",
    ]


async def test_queue_history_keeps_cancelled_delivered_user_message(session):
    """已送达后取消的用户消息仍属于对话历史，不能按排队消息隐藏。"""
    session.add(
        Conversation(
            id=1,
            thread_id="cancel-thread",
            project_id="project-thread-1",
            uid="user-1",
            agent_id="main",
            status="active",
        )
    )
    session.add(
        Message(
            id=1,
            conversation_id=1,
            role="user",
            content="请保留这条问题",
            request_id="cancel-request",
            run_id="cancel-run",
            delivery_status="cancelled",
        )
    )
    await session.commit()

    history = await get_thread_history_view(thread_id="cancel-thread", current_uid="user-1", db=session)

    assert [message["content"] for message in history["history"]] == ["请保留这条问题"]


async def test_thread_history_returns_run_timing_separately_from_messages(session):
    started_at = datetime(2026, 7, 12, 9, 0, 0)
    run_started_at = started_at + timedelta(seconds=10)
    run_prepared_at = started_at + timedelta(seconds=12)
    run_first_output_at = started_at + timedelta(seconds=16)
    run_finished_at = started_at + timedelta(seconds=22)
    session.add(
        Conversation(
            id=1,
            thread_id="thread-1",
            project_id="project-thread-1",
            uid="user-1",
            agent_id="main",
            status="active",
        )
    )
    session.add(
        AgentRun(
            id="run-a",
            conversation_thread_id="thread-1",
            runtime_scope_id="thread-1",
            agent_slug="main",
            uid="user-1",
            request_id="request-a",
            conversation_id=1,
            input_payload={},
            status="completed",
            created_at=started_at,
            started_at=run_started_at,
            prepared_at=run_prepared_at,
            first_output_at=run_first_output_at,
            finished_at=run_finished_at,
        )
    )
    session.add_all(
        [
            Message(
                id=1,
                conversation_id=1,
                role="user",
                content="A",
                request_id="request-a",
                run_id="run-a",
                delivery_status="complete",
                created_at=started_at,
            ),
            Message(
                id=2,
                conversation_id=1,
                role="assistant",
                content="A reply",
                run_id="run-a",
                delivery_status="complete",
                created_at=run_finished_at,
            ),
        ]
    )
    await session.commit()

    history = await get_thread_history_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=session,
    )

    assistant_message = next(message for message in history["history"] if message["type"] == "ai")
    assert assistant_message["run_id"] == "run-a"
    assert history["thread"]["id"] == "thread-1"
    assert history["thread"]["thread_status"] == "ready"
    assert len(history["runs"]) == 1
    assert history["runs"][0]["run_id"] == "run-a"
    assert history["runs"][0]["status"] == "completed"
    assert history["runs"][0]["timing"] == {
        "created_at": "2026-07-12T09:00:00Z",
        "started_at": "2026-07-12T09:00:10Z",
        "prepared_at": "2026-07-12T09:00:12Z",
        "first_model_request_at": None,
        "first_output_at": "2026-07-12T09:00:16Z",
        "finished_at": "2026-07-12T09:00:22Z",
        "dispatch_latency_ms": 10000,
        "preparation_latency_ms": 2000,
        "first_model_request_latency_ms": None,
        "model_first_output_latency_ms": 4000,
        "first_output_latency_ms": 16000,
        "total_latency_ms": 22000,
    }

    for message in history["history"]:
        assert {"run_started_at", "run_finished_at", "run_timing"}.isdisjoint(message)


async def test_thread_history_handles_run_without_timing_fields(session):
    started_at = datetime(2026, 7, 12, 9, 0, 0)
    session.add(
        Conversation(
            id=1,
            thread_id="thread-1",
            project_id="project-thread-1",
            uid="user-1",
            agent_id="main",
            status="active",
        )
    )
    session.add(
        AgentRun(
            id="run-a",
            conversation_thread_id="thread-1",
            runtime_scope_id="thread-1",
            agent_slug="main",
            uid="user-1",
            request_id="request-a",
            conversation_id=1,
            input_payload={},
            status="completed",
            created_at=started_at,
            started_at=None,
            finished_at=None,
        )
    )
    session.add(
        Message(
            id=1,
            conversation_id=1,
            role="assistant",
            content="A reply",
            run_id="run-a",
            delivery_status="complete",
            created_at=started_at,
        )
    )
    await session.commit()

    history = await get_thread_history_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=session,
    )
    assistant_message = next(message for message in history["history"] if message["type"] == "ai")
    assert "run_timing" not in assistant_message
    assert history["runs"][0]["timing"] == {
        "created_at": "2026-07-12T09:00:00Z",
        "started_at": None,
        "prepared_at": None,
        "first_model_request_at": None,
        "first_output_at": None,
        "finished_at": None,
        "dispatch_latency_ms": None,
        "preparation_latency_ms": None,
        "first_model_request_latency_ms": None,
        "model_first_output_latency_ms": None,
        "first_output_latency_ms": None,
        "total_latency_ms": None,
    }


async def test_thread_history_hides_internal_metadata_from_published_model_audit(session):
    """已发布 Model 输出保留产品 metadata，不暴露 lifecycle 字段。"""
    session.add(
        Conversation(
            id=1,
            thread_id="thread-1",
            project_id="project-thread-1",
            uid="user-1",
            agent_id="main",
            status="active",
        )
    )
    session.add(
        AgentRun(
            id="run-a",
            conversation_thread_id="thread-1",
            runtime_scope_id="thread-1",
            agent_slug="main",
            uid="user-1",
            request_id="request-a",
            conversation_id=1,
            input_payload={},
            status="interrupted",
        )
    )
    audit = Message(
        conversation_id=1,
        role="assistant",
        content="answer",
        message_type="text",
        extra_metadata={
            "state_reconciled": True,
            "model_run_id": "private-model-run",
            "start_metadata": {"provider": "private-provider"},
            "finish_metadata": {"model_name": "private-model"},
            "langfuse_trace_id": "trace-safe",
        },
        run_id="run-a",
        request_id="request-a",
        operation_id="model-a",
        execution_status="completed",
    )
    session.add(audit)
    await session.flush()
    session.add(
        ToolCall(
            message_id=audit.id,
            langgraph_tool_call_id="call-a",
            tool_name="search",
            tool_input={"q": "Yuxi"},
            tool_output="safe result",
            status="success",
        )
    )
    await session.commit()

    history = await get_thread_history_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=session,
    )

    assert len(history["history"]) == 1
    message = history["history"][0]
    assert message["extra_metadata"] == {"langfuse_trace_id": "trace-safe"}
    assert message["tool_calls"][0]["tool_call_result"] == {"content": "safe result"}
    assert "private-model-run" not in str(history)
    assert "private-provider" not in str(history)


async def test_history_citations_bind_tool_artifacts_to_the_owning_run(session):
    """同工具调用 ID 在不同 Run 重用时，历史原文不得串源。"""
    session.add(
        Conversation(
            id=1, thread_id="thread-1", project_id="project-thread-1", uid="user-1", agent_id="main", status="active"
        )
    )
    for index in (1, 2):
        run_id = f"run-{index}"
        source = {
            "source": f"kb://kb/{index}",
            "source_type": "file",
            "title": f"文件 {index}",
            "excerpts": [{"text": f"原文 {index}"}],
        }
        session.add(
            AgentRun(
                id=run_id,
                conversation_thread_id="thread-1",
                runtime_scope_id="thread-1",
                agent_slug="main",
                uid="user-1",
                request_id=run_id,
                conversation_id=1,
                input_payload={},
                status="completed",
            )
        )
        answer = Message(
            conversation_id=1,
            role="assistant",
            content=f'结论 {index}<cite source="kb://kb/{index}" type="file">1</cite>',
            message_type="text",
            run_id=run_id,
            operation_id="model",
            extra_metadata={"citation_sources": [source]},
            execution_status="completed",
        )
        session.add(answer)
        await session.flush()
        session.add(
            ToolCall(
                message_id=answer.id,
                langgraph_tool_call_id="same-call",
                tool_name="task",
                tool_output="摘要",
                status="success",
            )
        )
        session.add(
            Message(
                conversation_id=1,
                role="tool",
                content="摘要",
                message_type="tool_audit",
                run_id=run_id,
                operation_id="same-call",
                extra_metadata={"output": {"artifact": {"citation_sources": [source], "internal": "private-artifact"}}},
            )
        )
    legacy = Message(conversation_id=1, role="assistant", content="无标识的旧回复", message_type="text")
    session.add(legacy)
    await session.commit()
    history = (await get_thread_history_view(thread_id="thread-1", current_uid="user-1", db=session))["history"]
    assert "private-artifact" not in str(history)
    for item in history:
        if not item["run_id"]:
            assert item["content"] == legacy.content == "无标识的旧回复"
            continue
        expected_text = f"原文 {item['run_id'][-1]}"
        assert item["extra_metadata"]["citation_sources"][0]["excerpts"][0]["text"] == expected_text
        assert (
            item["tool_calls"][0]["tool_call_result"]["artifact"]["citation_sources"][0]["excerpts"][0]["text"]
            == expected_text
        )
