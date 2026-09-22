"""一次性 PG/Redis 中验证双恢复进程的锁竞争、连接预算和 FIFO 派发。"""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
from yuxi.services.run_queue_service import close_queue_clients, get_redis_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import AgentRunRequest, Conversation, Message, Project, User


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_recovery_processes_skip_192_locked_scopes_and_dispatch_fifo():
    """被锁线程不占满连接；解锁后每线程两请求按队头顺序各发布一次。"""
    if os.getenv("RECOVERY_SCAN_E2E") != "1":
        pytest.skip("使用 run_recovery_scan.sh 的一次性数据库")
    pg_manager.initialize()
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    processes, logs = [], []
    transaction = None
    directory = Path("/state/scan")
    directory.mkdir()
    try:
        assert await conn.fetchval("SELECT count(*) FROM users") == 0
        async with pg_manager.get_async_session_context() as db:
            db.add(User(username="recovery_test", uid="recovery_test", password_hash="synthetic"))
            await db.flush()
            for index in range(193):
                project = Project(
                    id=f"project-{index}",
                    uid="recovery_test",
                    selection_status="implicit",
                    workdir_path=f"projects/{uuid.uuid4()}",
                    directory_mode="managed",
                )
                db.add(project)
                await db.flush()
                conversation = Conversation(
                    thread_id=f"scope-{index}",
                    uid="recovery_test",
                    project_id=project.id,
                    agent_id="recovery-agent",
                    status="active",
                )
                db.add(conversation)
                await db.flush()
                for order in range(2):
                    message = Message(
                        conversation_id=conversation.id,
                        role="user",
                        content=f"request-{order}",
                        delivery_status="queued",
                    )
                    db.add(message)
                    await db.flush()
                    db.add(
                        AgentRunRequest(
                            request_id=f"request-{index}-{order}",
                            uid="recovery_test",
                            agent_slug="recovery-agent",
                            conversation_thread_id=conversation.thread_id,
                            status="queued",
                            input_message_id=message.id,
                            input_payload={},
                        )
                    )
            await db.commit()
        transaction = conn.transaction()
        await transaction.start()
        await conn.fetch("SELECT id FROM conversations WHERE thread_id <> 'scope-192' FOR UPDATE")
        for index in range(2):
            log = (directory / f"process-{index}.log").open("w")
            logs.append(log)
            processes.append(
                await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(Path(__file__).parents[2] / "e2e/runs/recovery_scan_process.py"),
                    str(directory),
                    str(index),
                    stdout=log,
                    stderr=log,
                )
            )

        async def wait_files(names):
            """等待独立进程的实际结果，提前报告进程错误。"""
            for _ in range(1500):
                if all((directory / name).exists() for name in names):
                    return
                for index, process in enumerate(processes):
                    if process.returncode not in (None, 0):
                        pytest.fail((directory / f"process-{index}.log").read_text()[-2000:])
                await asyncio.sleep(0.02)
            pytest.fail(f"恢复进程未完成: {names}")

        await wait_files([f"ready-{i}" for i in range(2)])
        (directory / "locked").touch()
        peak_connections, peak_waiters = 0, 0
        for _ in range(30):
            await asyncio.sleep(0.1)
            await conn.execute("SELECT pg_stat_clear_snapshot()")
            counts = await conn.fetchrow(
                "SELECT count(*) AS total,count(*) FILTER(WHERE wait_event_type='Lock') AS waiting "
                "FROM pg_stat_activity WHERE datname=current_database()"
            )
            peak_connections = max(peak_connections, counts["total"])
            peak_waiters = max(peak_waiters, counts["waiting"])
        finished_while_locked = all((directory / f"locked-{i}.json").exists() for i in range(2))
        unlocked_dispatched = await conn.fetchval(
            "SELECT count(*) FROM agent_run_requests WHERE conversation_thread_id='scope-192' AND status='dispatched'"
        )
        await transaction.rollback()
        transaction = None
        await wait_files([f"locked-{i}.json" for i in range(2)])
        (directory / "unlocked").touch()
        await wait_files([f"unlocked-{i}.json" for i in range(2)])
        rows = await conn.fetch(
            "SELECT q.request_id,q.status,q.dispatched_run_id,r.request_id AS run_request_id,"
            "m.run_id AS message_run_id "
            "FROM agent_run_requests q LEFT JOIN agent_runs r ON r.id=q.dispatched_run_id "
            "LEFT JOIN messages m ON m.id=q.input_message_id ORDER BY q.id"
        )
        first_run_ids = set()
        for row in rows:
            if row["request_id"].endswith("-0"):
                assert row["status"] == "dispatched"
                assert row["run_request_id"] == row["request_id"]
                assert row["message_run_id"] == row["dispatched_run_id"]
                first_run_ids.add(row["dispatched_run_id"])
            else:
                assert row["status"] == "queued" and row["dispatched_run_id"] is None
        assert len(first_run_ids) == 193
        redis = await get_redis_client()
        jobs = set(await redis.zrange("arq:queue", 0, -1))
        assert jobs == {f"run:{run_id}" for run_id in first_run_ids}
        # 本测试只验证派发，显式模拟前驱终态，不将其当作模型执行成功证据。
        await conn.execute("UPDATE agent_runs SET status='completed',finished_at=timezone('UTC',now())")
        (directory / "next").touch()
        await wait_files([f"next-{i}.json" for i in range(2)])
        for process in processes:
            assert await asyncio.wait_for(process.wait(), 10) == 0
        assert await conn.fetchval("SELECT count(*) FROM agent_run_requests WHERE status='dispatched'") == 386
        assert await conn.fetchval("SELECT count(DISTINCT request_id) FROM agent_runs") == 386
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM agent_run_requests q JOIN agent_runs r ON r.id=q.dispatched_run_id "
                "JOIN messages m ON m.id=q.input_message_id WHERE r.request_id=q.request_id AND m.run_id=r.id"
            )
            == 386
        )
        assert await redis.zcard("arq:queue") == 386
        metrics = [
            {
                phase: json.loads((directory / f"{phase}-{i}.json").read_text())
                for phase in ("locked", "unlocked", "next")
            }
            for i in range(2)
        ]
        print(
            json.dumps(
                {
                    "peak_connections": peak_connections,
                    "peak_lock_waiters": peak_waiters,
                    "finished_while_locked": finished_while_locked,
                    "processes": metrics,
                }
            )
        )
        assert finished_while_locked, "被锁线程阻断了整个恢复扫描"
        assert unlocked_dispatched == 1, "未被锁住的线程没有前进"
        assert peak_waiters == 0
        assert all(max(phase["peak"] for phase in metric.values()) <= 1 for metric in metrics)
        # 三个进程各有一个业务连接及四个默认 checkpoint 连接，另加独立观察连接。
        assert peak_connections <= 3 * (1 + 4) + 1
    finally:
        if transaction is not None:
            await transaction.rollback()
        for process in processes:
            if process.returncode is None:
                process.terminate()
                await process.wait()
        for log in logs:
            log.close()
        await conn.close()
        await close_queue_clients()
        await pg_manager.async_engine.dispose()
        await pg_manager.langgraph_pool.close()
