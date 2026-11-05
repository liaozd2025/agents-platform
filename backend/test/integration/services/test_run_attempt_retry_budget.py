"""真实 PostgreSQL 验证 Run attempt 序号不会被队列重投或重复领取重置。"""

import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test.integration.services.agent_run_test_helpers import create_agent_run
from yuxi.infrastructure.postgres.base import BusinessBase
from yuxi.infrastructure.postgres.models import load_models
from yuxi.modules.agents.models.runs import AgentRun, AgentRunAttempt
from yuxi.modules.agents.repositories.runs import AgentRunRepository
from yuxi.modules.agents.services import leases
from yuxi.modules.system.models import ConfigOption
from yuxi.modules.system.options import system_options

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


async def test_claim_returns_committed_attempt_and_redelivery_cannot_reset_history(monkeypatch):
    """领取返回持久序号；同 owner 续领、竞争者和终态重投都不增加次数。"""
    schema = "attempt_budget_" + uuid4().hex
    dsn = os.environ["POSTGRES_URL"]
    owner_engine = create_async_engine(dsn)
    engine = create_async_engine(dsn, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    load_models()

    @asynccontextmanager
    async def transaction():
        """模拟正式 session owner 的提交职责，使用实际 PostgreSQL。"""
        async with factory() as db, db.begin():
            yield db

    monkeypatch.setattr(leases.pg_manager, "get_async_session_context", transaction)
    try:
        async with owner_engine.begin() as db:
            await db.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as db:
            await db.run_sync(BusinessBase.metadata.create_all)
        async with factory() as db:
            db.add(ConfigOption(key=system_options.key, name=system_options.name, params=system_options.params, value={}))
            await db.commit()
        run_id, _thread_id, _message_id = await create_agent_run(
            factory,
            prefix="attempt-budget",
            message_content="test",
            input_payload={},
        )
        assert await leases.mark_run_running(run_id, "owner-1") == 1
        assert await leases.mark_run_running(run_id, "owner-1") == 1
        assert await leases.mark_run_running(run_id, "competing-owner") is None
        assert await leases.release_run_lease_for_retry(run_id, "owner-1")
        assert await leases.mark_run_running(run_id, "owner-2") is None
        async with factory() as db:
            run = await db.get(AgentRun, run_id)
            assert run.status == "pending" and run.runtime_cleanup_pending
            # 本例只领取 lease，没有启动执行环境；明确完成这个空执行的清理责任。
            run.runtime_cleanup_pending = False
            await db.commit()
        assert await leases.mark_run_running(run_id, "owner-2") == 2
        async with factory() as db:
            attempts = list(
                await db.scalars(select(AgentRunAttempt).where(AgentRunAttempt.run_id == run_id).order_by(AgentRunAttempt.attempt_no))
            )
            assert [attempt.attempt_no for attempt in attempts] == [1, 2]
            assert attempts[0].outcome == "retry_released" and attempts[0].finished_at is not None
            assert attempts[1].worker_id == "owner-2" and attempts[1].finished_at is None
            await AgentRunRepository(db).set_terminal_status(
                run_id,
                status="failed",
                worker_id="owner-2",
                error_type="run_retry_exhausted",
                error_message="test budget",
            )
            await db.commit()
        assert await leases.mark_run_running(run_id, "owner-3") is None
        async with factory() as db:
            attempts = await AgentRunRepository(db).list_run_attempts(run_id)
            assert [attempt.attempt_no for attempt in attempts] == [1, 2]
            assert attempts[-1].outcome == "failed" and attempts[-1].error_type == "run_retry_exhausted"
    finally:
        await engine.dispose()
        async with owner_engine.begin() as db:
            await db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await owner_engine.dispose()


async def test_recovery_skips_busy_thread_without_changing_realtime_locking(monkeypatch):
    """真实行锁占用时恢复可推进另一线程，实时提交仍等待原锁。"""
    import asyncio
    from types import SimpleNamespace

    from yuxi.modules.agents.repositories.sessions import SessionRepository
    from yuxi.modules.agents.services import scheduler

    schema = "recovery_lock_" + uuid4().hex
    dsn = os.environ["POSTGRES_URL"]
    owner_engine = create_async_engine(dsn)
    engine = create_async_engine(dsn, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    load_models()

    @asynccontextmanager
    async def transaction():
        """每次派发使用独立 PostgreSQL 事务。"""
        async with factory() as db, db.begin():
            yield db

    visited = []

    async def claim(**kwargs):
        """只观察取得 Thread 锁后的到达，不执行模型或创建文件。"""
        visited.append(kwargs["agent_session"].thread_id)
        return None

    async def binding(**kwargs):
        return SimpleNamespace(materialize_managed=False)

    monkeypatch.setattr(scheduler.pg_manager, "get_async_session_context", transaction)
    monkeypatch.setattr(scheduler, "claim_next_input", claim)
    monkeypatch.setattr(scheduler, "resolve_session_workdir_binding", binding)
    pending = None
    try:
        async with owner_engine.begin() as db:
            await db.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as db:
            await db.run_sync(BusinessBase.metadata.create_all)
        threads = []
        for index in range(2):
            run_id, thread_id, _ = await create_agent_run(factory, prefix=f"recovery-{index}", message_content="test", input_payload={})
            async with factory() as db:
                threads.append((thread_id, (await db.get(AgentRun, run_id)).uid))
        async with factory() as holder:
            await SessionRepository(holder).lock_session_by_thread_id(threads[0][0])
            for thread_id, uid in threads:
                assert await asyncio.wait_for(
                    scheduler.dispatch_next_input(uid=uid, agent_slug="main", thread_id=thread_id, skip_locked=True), 2
                )
            assert visited == [threads[1][0]]
            pending = asyncio.create_task(scheduler.dispatch_next_input(uid=threads[0][1], agent_slug="main", thread_id=threads[0][0]))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), 0.1)
            await holder.rollback()
            assert await asyncio.wait_for(pending, 2)
            assert visited == [threads[1][0], threads[0][0]]
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await engine.dispose()
        async with owner_engine.begin() as db:
            await db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await owner_engine.dispose()
