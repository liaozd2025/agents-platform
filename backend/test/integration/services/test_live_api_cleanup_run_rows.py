"""真实 PostgreSQL 上的 E2E 测试 run 行清理语义测试。"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test.live_api_cleanup import _delete_e2e_run_rows
from yuxi.storage.postgres.models_business import (
    AgentRun,
    AgentRunRequest,
    Conversation,
    Message,
    MessageFeedback,
    ToolCall,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture()
async def cleanup_database():
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _seed_thread(session_factory, *, thread_prefix: str) -> dict:
    """构造一个带输入消息、run、输出消息、tool_call、feedback 与请求的完整线程。"""
    thread_id = f"{thread_prefix}-{uuid.uuid4()}"
    uid = f"pytest-user-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    request_id = f"cleanup-req-{uuid.uuid4()}"
    async with session_factory() as db:
        conversation = Conversation(thread_id=thread_id, uid=uid, agent_id="main", status="active")
        db.add(conversation)
        await db.flush()
        input_message = Message(
            conversation_id=conversation.id,
            role="user",
            content="input",
            request_id=request_id,
            delivery_status="dispatched",
        )
        db.add(input_message)
        await db.flush()
        run = AgentRun(
            id=run_id,
            conversation_thread_id=thread_id,
            agent_slug="main",
            uid=uid,
            request_id=request_id,
            conversation_id=conversation.id,
            input_message_id=input_message.id,
            input_payload={},
            status="completed",
            run_type="chat",
        )
        db.add(run)
        await db.flush()
        output_message = Message(
            conversation_id=conversation.id,
            run_id=run_id,
            request_id=request_id,
            role="assistant",
            content="output",
            delivery_status="complete",
        )
        db.add(output_message)
        await db.flush()
        db.add(ToolCall(message_id=output_message.id, tool_name="fs", tool_input={}))
        db.add(MessageFeedback(message_id=output_message.id, uid=uid, rating="like"))
        db.add(
            AgentRunRequest(
                request_id=request_id,
                uid=uid,
                agent_slug="main",
                conversation_thread_id=thread_id,
                input_message_id=input_message.id,
                input_payload={},
                status="dispatched",
                dispatched_run_id=run_id,
            )
        )
        await db.commit()
        return {
            "thread_id": thread_id,
            "conversation_id": conversation.id,
            "run_id": run_id,
            "input_message_id": input_message.id,
            "output_message_id": output_message.id,
        }


async def _cleanup_seed(session_factory, seeds: list[dict]) -> None:
    async with session_factory() as db:
        conversation_ids = [seed["conversation_id"] for seed in seeds]
        run_ids = [seed["run_id"] for seed in seeds]
        message_ids = [
            message_id for seed in seeds for message_id in (seed["input_message_id"], seed["output_message_id"])
        ]
        await db.execute(delete(ToolCall).where(ToolCall.message_id.in_(message_ids)))
        await db.execute(delete(MessageFeedback).where(MessageFeedback.message_id.in_(message_ids)))
        await db.execute(delete(AgentRunRequest).where(AgentRunRequest.dispatched_run_id.in_(run_ids)))
        await db.execute(delete(Message).where(Message.id.in_(message_ids)))
        await db.execute(delete(AgentRun).where(AgentRun.id.in_(run_ids)))
        await db.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
        await db.commit()


async def test_delete_e2e_run_rows_removes_target_and_preserves_neighbor(cleanup_database):
    """目标线程的 run 及外键依赖全部删除、attempt 级联；相邻线程与无 run 消息保留。"""
    session_factory = cleanup_database
    target = await _seed_thread(session_factory, thread_prefix="pytest-cleanup-target")
    neighbor = await _seed_thread(session_factory, thread_prefix="pytest-cleanup-neighbor")

    try:
        await _delete_e2e_run_rows({target["thread_id"]})

        async with session_factory() as db:
            remaining_runs = set(
                (
                    await db.scalars(select(AgentRun.id).where(AgentRun.id.in_([target["run_id"], neighbor["run_id"]])))
                ).all()
            )
            remaining_target_messages = set(
                (
                    await db.scalars(
                        select(Message.id).where(
                            Message.id.in_([target["input_message_id"], target["output_message_id"]])
                        )
                    )
                ).all()
            )
            remaining_requests = await db.scalar(
                select(AgentRunRequest.id).where(AgentRunRequest.dispatched_run_id == target["run_id"])
            )
            remaining_tool_calls = await db.scalar(
                select(ToolCall.id).where(ToolCall.message_id == target["output_message_id"])
            )
            remaining_feedbacks = await db.scalar(
                select(MessageFeedback.id).where(MessageFeedback.message_id == target["output_message_id"])
            )
            neighbor_run = await db.get(AgentRun, neighbor["run_id"])
            neighbor_output = await db.get(Message, neighbor["output_message_id"])
            neighbor_input = await db.get(Message, neighbor["input_message_id"])
            target_conversation = await db.get(Conversation, target["conversation_id"])

        assert remaining_runs == {neighbor["run_id"]}
        assert remaining_target_messages == {target["input_message_id"]}
        assert remaining_requests is None
        assert remaining_tool_calls is None
        assert remaining_feedbacks is None
        assert neighbor_run is not None
        assert neighbor_output is not None
        assert neighbor_input is not None
        # 对话行由应用软删除生命周期管理，清理只删 run 级审计事实。
        assert target_conversation is not None
    finally:
        await _cleanup_seed(session_factory, [target, neighbor])


async def test_delete_e2e_run_rows_is_noop_for_unknown_threads(cleanup_database):
    """不存在的线程 id 不产生任何副作用。"""
    session_factory = cleanup_database
    seed = await _seed_thread(session_factory, thread_prefix="pytest-cleanup-unknown")

    try:
        await _delete_e2e_run_rows({"pytest-cleanup-does-not-exist"})

        async with session_factory() as db:
            run = await db.get(AgentRun, seed["run_id"])
            output = await db.get(Message, seed["output_message_id"])
            conversation = await db.get(Conversation, seed["conversation_id"])

        assert run is not None
        assert output is not None
        assert conversation is not None
    finally:
        await _cleanup_seed(session_factory, [seed])


async def test_delete_e2e_run_rows_is_idempotent(cleanup_database):
    """重复执行同一清理不报错（事务内删除，第二次命中 0 行）。"""
    session_factory = cleanup_database
    target = await _seed_thread(session_factory, thread_prefix="pytest-cleanup-idem")

    try:
        await _delete_e2e_run_rows({target["thread_id"]})
        await _delete_e2e_run_rows({target["thread_id"]})

        async with session_factory() as db:
            remaining = await db.get(AgentRun, target["run_id"])

        assert remaining is None
    finally:
        await _cleanup_seed(session_factory, [target])
