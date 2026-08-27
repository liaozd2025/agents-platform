"""真实 PostgreSQL 上的 AgentRun 运行清单与 RunAttempt 事实测试。"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.services.agent_run_manifest_service import compute_manifest_fingerprint
from yuxi.storage.postgres.manager import AGENT_RUN_FACT_SCHEMA_STATEMENTS
from yuxi.storage.postgres.models_business import AgentRun, AgentRunAttempt, Conversation, Message
from yuxi.utils.datetime_utils import utc_now_naive

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture()
async def fact_database():
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    async with engine.begin() as connection:
        for _ in range(2):
            for statement in AGENT_RUN_FACT_SCHEMA_STATEMENTS:
                await connection.execute(text(statement))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield engine, session_factory
    finally:
        await engine.dispose()


async def _create_run(session_factory, *, status: str = "pending") -> tuple[str, str]:
    run_id = str(uuid.uuid4())
    request_id = f"fact-{uuid.uuid4()}"
    thread_id = f"pytest-fact-{uuid.uuid4()}"
    uid = f"pytest-user-{uuid.uuid4()}"
    async with session_factory() as db:
        conversation = Conversation(thread_id=thread_id, uid=uid, agent_id="main", status="active")
        db.add(conversation)
        await db.flush()
        message = Message(
            conversation_id=conversation.id,
            role="user",
            content="fact input",
            request_id=request_id,
            delivery_status="dispatched",
        )
        db.add(message)
        await db.flush()
        db.add(
            AgentRun(
                id=run_id,
                conversation_thread_id=thread_id,
                agent_slug="main",
                uid=uid,
                request_id=request_id,
                conversation_id=conversation.id,
                input_message_id=message.id,
                input_payload={"model_spec": "provider/model-a"},
                status=status,
                run_type="chat",
            )
        )
        await db.commit()
        return run_id, thread_id


async def _cleanup_runs(session_factory, thread_ids: list[str]) -> None:
    async with session_factory() as db:
        conversation_ids = list(
            (await db.scalars(select(Conversation.id).where(Conversation.thread_id.in_(thread_ids)))).all()
        )
        if conversation_ids:
            await db.execute(delete(Message).where(Message.conversation_id.in_(conversation_ids)))
        await db.execute(delete(AgentRun).where(AgentRun.conversation_thread_id.in_(thread_ids)))
        await db.execute(delete(Conversation).where(Conversation.thread_id.in_(thread_ids)))
        await db.commit()


async def _persisted_attempts(session_factory, run_id: str) -> list[AgentRunAttempt]:
    async with session_factory() as db:
        return await AgentRunRepository(db).list_run_attempts(run_id)


async def test_run_fact_schema_evolution_is_idempotent(fact_database):
    engine, _ = fact_database
    async with engine.connect() as connection:
        columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'agent_runs' "
                        "AND column_name IN ('manifest', 'manifest_fingerprint', 'manifest_recorded_at')"
                    )
                )
            ).scalars()
        )
        attempt_table_exists = await connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_run_attempts')")
        )
        attempt_columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'agent_run_attempts' "
                        "AND column_name IN ('adapter', 'instance_id', 'route_reason', 'route_snapshot', "
                        "'runtime_manifest', 'runtime_manifest_digest', 'result_events', 'final_acked_at', "
                        "'cleanup_error', 'cleanup_failed_at')"
                    )
                )
            ).scalars()
        )
        unique_index_exists = await connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_indexes "
                "WHERE tablename = 'agent_run_attempts' "
                "AND indexname = 'uq_agent_run_attempts_run_attempt_no')"
            )
        )

    assert columns == {"manifest", "manifest_fingerprint", "manifest_recorded_at"}
    assert attempt_table_exists is True
    assert attempt_columns == {
        "adapter",
        "instance_id",
        "route_reason",
        "route_snapshot",
        "runtime_manifest",
        "runtime_manifest_digest",
        "result_events",
        "final_acked_at",
        "cleanup_error",
        "cleanup_failed_at",
    }
    assert unique_index_exists is True


async def test_attempt_history_survives_retry_takeover_and_reconciliation(fact_database):
    """重试、接管与失联收敛各自留下不可改写的 attempt 事实。"""
    _, session_factory = fact_database
    now = utc_now_naive()
    owner_a = "worker-a:token-1"
    owner_b = "worker-b:token-2"
    run_id, thread_id = await _create_run(session_factory)

    try:
        async with session_factory() as db:
            repository = AgentRunRepository(db)
            _, first_claim = await repository.mark_running(run_id, worker_id=owner_a, lease_seconds=60, now=now)
            await db.commit()
        assert first_claim is True

        async with session_factory() as db:
            repository = AgentRunRepository(db)
            renewed = await repository.renew_lease(
                run_id, worker_id=owner_a, lease_seconds=60, now=now + timedelta(seconds=10)
            )
            await db.commit()
        assert renewed is True

        async with session_factory() as db:
            repository = AgentRunRepository(db)
            released = await repository.release_lease_for_retry(
                run_id, worker_id=owner_a, now=now + timedelta(seconds=11)
            )
            await db.commit()
        assert released is True

        async with session_factory() as db:
            repository = AgentRunRepository(db)
            _, second_claim = await repository.mark_running(
                run_id, worker_id=owner_b, lease_seconds=5, now=now + timedelta(seconds=12)
            )
            await db.commit()
        assert second_claim is True

        reconciled_at = now + timedelta(seconds=30)
        async with session_factory() as db:
            repository = AgentRunRepository(db)
            reconciled = await repository.reconcile_expired_leases(now=reconciled_at)
            await db.commit()

        attempts = await _persisted_attempts(session_factory, run_id)

        assert [run.id for run in reconciled] == [run_id]
        assert [attempt.attempt_no for attempt in attempts] == [1, 2]
        first, second = attempts
        assert first.worker_id == owner_a
        assert first.outcome == "retry_released"
        assert first.finished_at is not None
        assert first.finished_at < second.finished_at
        assert first.heartbeat_at == now + timedelta(seconds=10)
        assert second.worker_id == owner_b
        assert second.outcome == "lease_expired"
        assert second.error_type == "worker_lease_expired"
        assert second.finished_at == reconciled_at
    finally:
        await _cleanup_runs(session_factory, [thread_id])


async def test_concurrent_claims_produce_single_valid_attempt(fact_database):
    """真实行锁下并发 claim 只有一个 attempt 获得有效执行权。"""
    _, session_factory = fact_database
    now = utc_now_naive()
    run_id, thread_id = await _create_run(session_factory)

    async def claim(worker_id: str) -> bool:
        async with session_factory() as db:
            repository = AgentRunRepository(db)
            _, acquired = await repository.mark_running(run_id, worker_id=worker_id, lease_seconds=60, now=now)
            await db.commit()
            return acquired

    try:
        results = await asyncio.gather(
            claim("worker-race:token-1"),
            claim("worker-race:token-2"),
            claim("worker-race:token-3"),
        )
        attempts = await _persisted_attempts(session_factory, run_id)

        assert results.count(True) == 1
        assert len(attempts) == 1
        assert attempts[0].attempt_no == 1
        assert attempts[0].worker_id in {"worker-race:token-1", "worker-race:token-2", "worker-race:token-3"}
        assert attempts[0].finished_at is None
    finally:
        await _cleanup_runs(session_factory, [thread_id])


async def test_duplicate_attempt_no_rejected_by_unique_constraint(fact_database):
    """(run_id, attempt_no) 唯一约束是执行占有事实的数据库级失败面。"""
    _, session_factory = fact_database
    now = utc_now_naive()
    run_id, thread_id = await _create_run(session_factory)

    try:
        async with session_factory() as db:
            repository = AgentRunRepository(db)
            _, acquired = await repository.mark_running(
                run_id, worker_id="worker-uq:token-1", lease_seconds=60, now=now
            )
            await db.commit()
        assert acquired is True

        async with session_factory() as db:
            db.add(
                AgentRunAttempt(
                    run_id=run_id,
                    attempt_no=1,
                    worker_id="worker-forged:token-x",
                    started_at=now,
                )
            )
            with pytest.raises(IntegrityError):
                await db.flush()
            await db.rollback()

        attempts = await _persisted_attempts(session_factory, run_id)
        assert [attempt.worker_id for attempt in attempts] == ["worker-uq:token-1"]
    finally:
        await _cleanup_runs(session_factory, [thread_id])


async def test_manifest_write_once_keeps_original_fingerprint_after_config_change(fact_database):
    """配置变化后重放不能改写历史 Run 的 manifest 与指纹。"""
    _, session_factory = fact_database
    now = utc_now_naive()
    owner = "worker-manifest:token-1"
    run_id, thread_id = await _create_run(session_factory)

    try:
        async with session_factory() as db:
            repository = AgentRunRepository(db)
            await repository.mark_running(run_id, worker_id=owner, lease_seconds=60, now=now)
            await db.commit()

        original_manifest = {"manifest_version": 1, "model": {"spec": "provider/model-a"}}
        original_fingerprint = "a" * 64
        async with session_factory() as db:
            repository = AgentRunRepository(db)
            _, recorded = await repository.record_run_manifest(
                run_id,
                manifest=original_manifest,
                fingerprint=original_fingerprint,
                worker_id=owner,
                now=now + timedelta(seconds=1),
            )
            await db.commit()
        assert recorded is True

        async with session_factory() as db:
            repository = AgentRunRepository(db)
            _, rewritten = await repository.record_run_manifest(
                run_id,
                manifest={"manifest_version": 1, "model": {"spec": "provider/model-changed"}},
                fingerprint="b" * 64,
                worker_id=owner,
                now=now + timedelta(seconds=2),
            )
            await db.commit()
        assert rewritten is False

        async with session_factory() as db:
            persisted_run = await db.get(AgentRun, run_id)

        assert persisted_run.manifest == original_manifest
        assert persisted_run.manifest_fingerprint == original_fingerprint
        assert persisted_run.manifest_recorded_at == now + timedelta(seconds=1)
    finally:
        await _cleanup_runs(session_factory, [thread_id])


async def test_manifest_rejects_stale_owner_and_expired_lease(fact_database):
    """非 owner 或过期 lease 不能固化 manifest；历史 Run 的 NULL 保持 unknown。"""
    _, session_factory = fact_database
    now = utc_now_naive()
    run_id, thread_id = await _create_run(session_factory)
    legacy_run_id, legacy_thread_id = await _create_run(session_factory)

    try:
        async with session_factory() as db:
            repository = AgentRunRepository(db)
            await repository.mark_running(run_id, worker_id="worker-live:token-1", lease_seconds=60, now=now)
            await db.commit()

        async with session_factory() as db:
            repository = AgentRunRepository(db)
            with pytest.raises(ValueError, match="lease owner"):
                await repository.record_run_manifest(
                    run_id,
                    manifest={"manifest_version": 1},
                    fingerprint="a" * 64,
                    worker_id="worker-stale:token-2",
                    now=now + timedelta(seconds=1),
                )
            await db.rollback()

        async with session_factory() as db:
            repository = AgentRunRepository(db)
            with pytest.raises(ValueError, match="lease owner"):
                await repository.record_run_manifest(
                    run_id,
                    manifest={"manifest_version": 1},
                    fingerprint="a" * 64,
                    worker_id="worker-live:token-1",
                    now=now + timedelta(seconds=61),
                )
            await db.rollback()

        async with session_factory() as db:
            active_run = await db.get(AgentRun, run_id)
            legacy_run = await db.get(AgentRun, legacy_run_id)

        assert active_run.manifest is None
        assert active_run.manifest_fingerprint is None
        # 历史 Run 未固化 manifest 的事实保持 unknown，不会被补写。
        assert legacy_run.manifest is None
        assert legacy_run.manifest_recorded_at is None
        assert await _persisted_attempts(session_factory, legacy_run_id) == []
    finally:
        await _cleanup_runs(session_factory, [thread_id, legacy_thread_id])


async def test_pi_attempt_freezes_route_and_runtime_manifest_at_claim(fact_database):
    """PI adapter 与 Runtime Manifest 只在初次 claim 写入，不被续租改写。"""
    _, session_factory = fact_database
    now = utc_now_naive()
    owner = "worker-pi:token-1"
    run_id, thread_id = await _create_run(session_factory)
    manifest = {"manifest_version": 1, "runner": {"protocol": "yuxi.pi-jsonl.v1"}}
    metadata = {
        "adapter": "local",
        "route_reason": "t2_local_only",
        "route_snapshot": {"rule_version": "t2"},
        "runtime_manifest": manifest,
        "runtime_manifest_digest": "a" * 64,
    }

    try:
        async with session_factory() as db:
            repository = AgentRunRepository(db)
            _, acquired = await repository.mark_running(
                run_id,
                worker_id=owner,
                lease_seconds=60,
                now=now,
                attempt_metadata=metadata,
            )
            await db.commit()
        assert acquired is True

        async with session_factory() as db:
            repository = AgentRunRepository(db)
            _, renewed = await repository.mark_running(
                run_id,
                worker_id=owner,
                lease_seconds=60,
                now=now + timedelta(seconds=1),
                attempt_metadata={**metadata, "adapter": "forged"},
            )
            await db.commit()
        assert renewed is True

        attempt = (await _persisted_attempts(session_factory, run_id))[0]
        async with session_factory() as db:
            repository = AgentRunRepository(db)
            assert (
                await repository.bind_pi_instance(
                    run_id,
                    attempt_id=attempt.id,
                    instance_id="sandbox-1",
                    worker_id=owner,
                    now=now + timedelta(seconds=2),
                )
                is True
            )
            assert (
                await repository.bind_pi_instance(
                    run_id,
                    attempt_id=attempt.id,
                    instance_id="sandbox-1",
                    worker_id=owner,
                    now=now + timedelta(seconds=3),
                )
                is False
            )
            with pytest.raises(ValueError, match="instance"):
                await repository.bind_pi_instance(
                    run_id,
                    attempt_id=attempt.id,
                    instance_id="forged",
                    worker_id=owner,
                    now=now + timedelta(seconds=4),
                )
            await db.commit()
        attempt = (await _persisted_attempts(session_factory, run_id))[0]
        assert attempt.adapter == "local"
        assert attempt.instance_id == "sandbox-1"
        assert attempt.route_reason == "t2_local_only"
        assert attempt.route_snapshot == {"rule_version": "t2"}
        assert attempt.runtime_manifest == manifest
        assert attempt.runtime_manifest_digest == "a" * 64
    finally:
        await _cleanup_runs(session_factory, [thread_id])


async def test_pi_envelope_replay_is_idempotent_and_final_ack_is_durable(fact_database):
    """final 重放只生成一个 Message，ACK 后 PostgreSQL 结果仍可回读。"""
    _, session_factory = fact_database
    now = utc_now_naive()
    owner = "worker-pi:token-1"
    run_id, thread_id = await _create_run(session_factory)
    manifest_digest = "a" * 64
    metadata = {
        "adapter": "local",
        "route_reason": "t2_local_only",
        "route_snapshot": {"rule_version": "t2"},
        "runtime_manifest": {"manifest_version": 1},
        "runtime_manifest_digest": manifest_digest,
    }
    final = {
        "job_id": run_id,
        "attempt_id": "pending",
        "adapter": "local",
        "event_id": "final-1",
        "sequence": 3,
        "type": "final",
        "runtime_manifest_digest": manifest_digest,
        "payload": {
            "text": "YUXI_PI_GOLDEN_V1",
            "artifact": {"path": "pi-golden.txt", "sha256": "b" * 64},
            "session": {"id": "session-1"},
        },
        "payload_digest": "pending",
    }
    final["payload_digest"] = compute_manifest_fingerprint(final["payload"])

    try:
        async with session_factory() as db:
            repository = AgentRunRepository(db)
            await repository.mark_running(
                run_id,
                worker_id=owner,
                lease_seconds=60,
                now=now,
                attempt_metadata=metadata,
            )
            await db.commit()
        attempt = (await _persisted_attempts(session_factory, run_id))[0]
        final["attempt_id"] = str(attempt.id)
        log = {
            "job_id": run_id,
            "attempt_id": str(attempt.id),
            "adapter": "local",
            "event_id": "log-1",
            "sequence": 0,
            "type": "log",
            "runtime_manifest_digest": manifest_digest,
            "payload": {"message": "pi_started"},
            "payload_digest": compute_manifest_fingerprint({"message": "pi_started"}),
        }

        async with session_factory() as db:
            log_ack = await AgentRunRepository(db).record_pi_envelope(
                run_id,
                attempt_id=attempt.id,
                envelope=log,
                worker_id=owner,
                now=now + timedelta(milliseconds=500),
            )
            await db.commit()
        assert log_ack == {"ack": True, "duplicate": False}

        async with session_factory() as db:
            ack = await AgentRunRepository(db).record_pi_envelope(
                run_id,
                attempt_id=attempt.id,
                envelope=final,
                worker_id=owner,
                now=now + timedelta(seconds=1),
            )
            await db.commit()
        assert ack == {"ack": True, "duplicate": False}

        async with session_factory() as db:
            replay_ack = await AgentRunRepository(db).record_pi_envelope(
                run_id,
                attempt_id=attempt.id,
                envelope=final,
                worker_id=owner,
                now=now + timedelta(seconds=2),
            )
            await db.commit()
            persisted_run = await db.get(AgentRun, run_id)
            messages = list((await db.scalars(select(Message).where(Message.run_id == run_id))).all())
            persisted_attempt = await db.get(AgentRunAttempt, attempt.id)

        assert replay_ack == {"ack": True, "duplicate": True}
        assert persisted_run.status == "completed"
        assert persisted_run.output_message_id == messages[0].id
        assert [message.content for message in messages] == ["YUXI_PI_GOLDEN_V1"]
        assert messages[0].extra_metadata["pi"]["artifact"]["path"] == "pi-golden.txt"
        assert persisted_attempt.result_events == [log, final]
        assert persisted_attempt.final_acked_at == now + timedelta(seconds=1)

        async with session_factory() as db:
            await AgentRunRepository(db).record_pi_cleanup_failure(
                run_id,
                attempt_id=attempt.id,
                worker_id=owner,
                error_message="provisioner unavailable",
                now=now + timedelta(seconds=3),
            )
            await db.commit()
            persisted_attempt = await db.get(AgentRunAttempt, attempt.id)

        assert persisted_attempt.error_type is None
        assert persisted_attempt.cleanup_error == "provisioner unavailable"
        assert persisted_attempt.cleanup_failed_at == now + timedelta(seconds=3)
    finally:
        await _cleanup_runs(session_factory, [thread_id])


async def test_stale_pi_attempt_cannot_write_event(fact_database):
    """已释放的旧 attempt 不能向当前 Run 增加结果。"""
    _, session_factory = fact_database
    now = utc_now_naive()
    run_id, thread_id = await _create_run(session_factory)
    metadata = {
        "adapter": "local",
        "route_reason": "t2_local_only",
        "route_snapshot": {"rule_version": "t2"},
        "runtime_manifest": {"manifest_version": 1},
        "runtime_manifest_digest": "a" * 64,
    }

    try:
        async with session_factory() as db:
            repository = AgentRunRepository(db)
            await repository.mark_running(
                run_id,
                worker_id="worker-old:token-1",
                lease_seconds=60,
                now=now,
                attempt_metadata=metadata,
            )
            await repository.release_lease_for_retry(
                run_id,
                worker_id="worker-old:token-1",
                now=now + timedelta(seconds=1),
            )
            await repository.mark_running(
                run_id,
                worker_id="worker-new:token-2",
                lease_seconds=60,
                now=now + timedelta(seconds=2),
                attempt_metadata=metadata,
            )
            await db.commit()
        old_attempt = (await _persisted_attempts(session_factory, run_id))[0]
        envelope = {
            "job_id": run_id,
            "attempt_id": str(old_attempt.id),
            "adapter": "local",
            "event_id": "stale-log",
            "sequence": 0,
            "type": "log",
            "runtime_manifest_digest": "a" * 64,
            "payload": {"message": "late"},
            "payload_digest": compute_manifest_fingerprint({"message": "late"}),
        }

        async with session_factory() as db:
            with pytest.raises(ValueError, match="当前 PI attempt"):
                await AgentRunRepository(db).record_pi_envelope(
                    run_id,
                    attempt_id=old_attempt.id,
                    envelope=envelope,
                    worker_id="worker-old:token-1",
                    now=now + timedelta(seconds=3),
                )
            await db.rollback()
    finally:
        await _cleanup_runs(session_factory, [thread_id])
