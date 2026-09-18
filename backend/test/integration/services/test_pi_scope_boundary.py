"""真实 PostgreSQL 上验证 PI 创建授权和共享执行域的事务边界。"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.agent_repository import DEFAULT_SHARE_CONFIG
from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.services import pi_sandbox_run_service as svc
from yuxi.storage.postgres.models_business import (
    Agent,
    AgentRun,
    AgentRunAttempt,
    Conversation,
    ConversationStats,
    Message,
    Project,
    SubagentThread,
    User,
)
from yuxi.utils.datetime_utils import utc_now_naive

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture()
async def pi_scope_database(monkeypatch):
    """每个场景创建独立用户、Project、根 Run 和两个普通子图。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    uid = f"pytest-pi-{uuid.uuid4().hex}"
    project_id = str(uuid.uuid4())
    root_id = str(uuid.uuid4())
    scope = f"pytest-pi-root-{uuid.uuid4().hex}"
    child_ids = []
    monkeypatch.setattr(svc.agent_run_service.agent_manager, "get_agent", lambda _backend: object())
    async with sessions() as db:
        db.add(User(username=uid, uid=uid, password_hash="test"))
        await db.flush()
        db.add(
            Project(
                id=project_id,
                uid=uid,
                selection_status="implicit",
                directory_mode="managed",
                workdir_path=f"projects/{project_id}",
            )
        )
        await db.flush()
        root_conversation = Conversation(thread_id=scope, project_id=project_id, uid=uid, agent_id=uid, status="active")
        db.add(root_conversation)
        db.add(Agent(slug=uid, backend_id="test", name="PI 根图", created_by=uid, share_config=DEFAULT_SHARE_CONFIG))
        await db.flush()
        db.add(
            AgentRun(
                id=root_id,
                conversation_thread_id=scope,
                runtime_scope_id=scope,
                agent_slug=uid,
                uid=uid,
                request_id=str(uuid.uuid4()),
                conversation_id=root_conversation.id,
                run_type="chat",
                status="running",
                worker_id="fixture-root-owner",
                heartbeat_at=utc_now_naive(),
                lease_expires_at=utc_now_naive() + timedelta(hours=1),
                input_payload={"model_spec": "test:model", "tool_approval_mode": "always_trust"},
            )
        )
        for index in range(2):
            slug = f"{uid}-{index}"
            thread_id = f"pytest-pi-sub-{uuid.uuid4().hex}"
            child_id = str(uuid.uuid4())
            conversation = Conversation(
                thread_id=thread_id, project_id=project_id, uid=uid, agent_id=slug, status="subagent"
            )
            db.add(conversation)
            db.add(
                Agent(
                    slug=slug,
                    backend_id="test",
                    name="PI 子图",
                    created_by=uid,
                    is_subagent=True,
                    share_config=DEFAULT_SHARE_CONFIG,
                )
            )
            await db.flush()
            relation = SubagentThread(
                uid=uid,
                parent_conversation_id=root_conversation.id,
                child_conversation_id=conversation.id,
                child_thread_id=thread_id,
                subagent_slug=slug,
                created_by_run_id=root_id,
            )
            db.add(relation)
            await db.flush()
            db.add(
                AgentRun(
                    id=child_id,
                    conversation_thread_id=thread_id,
                    runtime_scope_id=scope,
                    agent_slug=slug,
                    uid=uid,
                    request_id=str(uuid.uuid4()),
                    conversation_id=conversation.id,
                    run_type="subagent",
                    status="running",
                    worker_id=f"fixture-subagent-{index}",
                    heartbeat_at=utc_now_naive(),
                    lease_expires_at=utc_now_naive() + timedelta(hours=1),
                    created_by_run_id=root_id,
                    subagent_thread_relation_id=relation.id,
                    input_payload={"model_spec": "test:model", "tool_approval_mode": "always_trust"},
                )
            )
            child_ids.append(child_id)
        await db.commit()
    try:
        yield sessions, uid, project_id, root_id, child_ids
    finally:
        async with sessions() as db:
            conversation_ids = select(Conversation.id).where(Conversation.uid == uid)
            await db.execute(delete(Message).where(Message.conversation_id.in_(conversation_ids)))
            await db.execute(delete(AgentRun).where(AgentRun.uid == uid))
            await db.execute(delete(SubagentThread).where(SubagentThread.uid == uid))
            await db.execute(delete(ConversationStats).where(ConversationStats.conversation_id.in_(conversation_ids)))
            await db.execute(delete(Conversation).where(Conversation.uid == uid))
            await db.execute(delete(Agent).where(Agent.created_by == uid))
            await db.execute(delete(Project).where(Project.uid == uid))
            await db.execute(delete(User).where(User.uid == uid))
            await db.commit()
        await engine.dispose()


async def _start(sessions, uid, creator_id, tool_call_id="pi-call"):
    """通过真实创建服务登记 PI，不连接模型或执行文件操作。"""
    async with sessions() as db:
        return await svc.PiSandboxRunService(db).start(
            uid=uid,
            created_by_run_id=creator_id,
            description="生成报表",
            tool_call_id=tool_call_id,
            skill_slugs=[],
            skill_sources={},
            skill_runtime_paths={},
        )


async def test_parallel_subagents_cannot_register_two_pi_runs_in_shared_scope(pi_scope_database, monkeypatch):
    sessions, uid, _project_id, _root_id, child_ids = pi_scope_database
    first_locked = asyncio.Event()
    release_first = asyncio.Event()

    async def hold_first_registration(_sources):
        first_locked.set()
        await release_first.wait()
        return {}

    monkeypatch.setattr(svc, "_compute_skill_digests", hold_first_registration)
    first = asyncio.create_task(_start(sessions, uid, child_ids[0]))
    await asyncio.wait_for(first_locked.wait(), timeout=5)
    second = asyncio.create_task(_start(sessions, uid, child_ids[1]))
    await asyncio.sleep(0.05)
    assert not second.done()
    release_first.set()
    results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), timeout=10)
    assert sum(isinstance(result, svc.PiSandboxStartResult) for result in results) == 1
    assert sum(isinstance(result, ValueError) and "共享运行域" in str(result) for result in results) == 1
    async with sessions() as db:
        runs = list(
            (await db.scalars(select(AgentRun).where(AgentRun.uid == uid, AgentRun.run_type == "sandbox"))).all()
        )
        assert len(runs) == 1 and runs[0].status == "pending"


@pytest.mark.parametrize("error_type", ["execution_unknown", "cleanup_failed", "result_persistence_failed"])
async def test_pi_failure_cannot_be_bypassed_through_sibling_subagent(pi_scope_database, error_type):
    sessions, uid, _project_id, root_id, child_ids = pi_scope_database
    original = await _start(sessions, uid, child_ids[0])
    async with sessions() as db:
        run = await db.get(AgentRun, original.run.id)
        run.status, run.error_type = "failed", error_type
        await db.commit()
    replay = await _start(sessions, uid, child_ids[0])
    assert not replay.created and replay.run.id == original.run.id
    for creator_id in (root_id, child_ids[1]):
        with pytest.raises(ValueError, match=f"共享运行域.*{original.run.id}"):
            await _start(sessions, uid, creator_id, "another-pi")
    async with sessions() as db:
        runs = list(
            (await db.scalars(select(AgentRun).where(AgentRun.uid == uid, AgentRun.run_type == "sandbox"))).all()
        )
        assert [run.id for run in runs] == [original.run.id]


async def test_default_subagent_direct_creation_is_denied_and_main_graph_remains_available(pi_scope_database):
    sessions, uid, _project_id, root_id, child_ids = pi_scope_database
    async with sessions() as db:
        for run_id in (root_id, child_ids[0]):
            run = await db.get(AgentRun, run_id)
            run.input_payload = {**run.input_payload, "tool_approval_mode": "default"}
        await db.commit()
    with pytest.raises(ValueError, match="交回主智能体"):
        await _start(sessions, uid, child_ids[0])
    async with sessions() as db:
        assert (
            list((await db.scalars(select(AgentRun).where(AgentRun.uid == uid, AgentRun.run_type == "sandbox"))).all())
            == []
        )
    approved_main = await _start(sessions, uid, root_id)
    assert approved_main.created and approved_main.run.created_by_run_id == root_id


async def test_pi_cleanup_failure_blocks_new_scope_work_until_attempt_is_cleared(pi_scope_database):
    sessions, uid, _project_id, root_id, child_ids = pi_scope_database
    original = await _start(sessions, uid, child_ids[0])
    async with sessions() as db:
        repo = AgentRunRepository(db)
        await repo.mark_running(
            original.run.id,
            worker_id="owner",
            lease_seconds=60,
            attempt_metadata={
                "adapter": "local",
                "runtime_manifest": {"manifest_version": 1},
                "runtime_manifest_digest": "a" * 64,
                "route_reason": "test",
                "route_snapshot": {"rule_version": "test"},
            },
        )
        attempt = (await repo.list_run_attempts(original.run.id))[0]
        failed_at = utc_now_naive()
        await repo.record_pi_cleanup_failure(
            original.run.id, attempt_id=attempt.id, worker_id="owner", error_message="cleanup uncertain", now=failed_at
        )
        await repo.set_terminal_status(original.run.id, status="failed", error_type="model_failed", worker_id="owner")
        await db.commit()
        attempt_id = attempt.id
    with pytest.raises(ValueError, match="共享运行域"):
        await _start(sessions, uid, child_ids[1])
    async with sessions() as db:
        assert await AgentRunRepository(db).clear_pi_cleanup_failure(attempt_id, failed_at=failed_at)
        await db.commit()
        assert (await db.get(AgentRunAttempt, attempt_id)).cleanup_failed_at is None
    successor = await _start(sessions, uid, child_ids[1])
    assert successor.created and successor.run.id != original.run.id


@pytest.mark.parametrize("failure", ["worker_lease_expired", "sandbox_parent_unavailable"])
async def test_reconciled_unknown_pi_blocks_live_sibling_execution(pi_scope_database, failure):
    """真实收敛仅令 PI 结果未知，仍有效的根图及兄弟不能重新委派。"""
    sessions, uid, _project_id, root_id, child_ids = pi_scope_database
    now = utc_now_naive()
    original = await _start(sessions, uid, child_ids[0])
    async with sessions() as db:
        for run_id in (root_id, *child_ids):
            run = await db.get(AgentRun, run_id)
            run.worker_id = f"owner-{run_id}"
            run.heartbeat_at = now
            run.lease_expires_at = now + timedelta(hours=1)
        repo = AgentRunRepository(db)
        if failure == "worker_lease_expired":
            await repo.mark_running(original.run.id, worker_id="lost-pi-owner", lease_seconds=1, now=now)
        else:
            creator = await db.get(AgentRun, child_ids[0])
            creator.status = "failed"
        await db.commit()
        reconciled, _ = await repo.reconcile_expired_leases(now=now + timedelta(seconds=2))
        assert [run.id for run in reconciled] == [original.run.id]
        await db.commit()

    async with sessions() as db:
        failed = await db.get(AgentRun, original.run.id)
        assert failed.status == "failed" and failed.error_type == failure
        for run_id in (root_id, child_ids[1]):
            live = await db.get(AgentRun, run_id)
            assert live.status == "running" and live.lease_expires_at > now + timedelta(seconds=2)
        if failure == "worker_lease_expired":
            attempts = await AgentRunRepository(db).list_run_attempts(original.run.id)
            assert len(attempts) == 1 and attempts[0].error_type == failure and attempts[0].finished_at is not None
    with pytest.raises(ValueError, match=f"共享运行域.*{original.run.id}"):
        await _start(sessions, uid, child_ids[1], "retry-after-reconciliation")
    async with sessions() as db:
        persisted_ids = list(
            (await db.scalars(select(AgentRun.id).where(AgentRun.uid == uid, AgentRun.run_type == "sandbox"))).all()
        )
        assert persisted_ids == [original.run.id]
