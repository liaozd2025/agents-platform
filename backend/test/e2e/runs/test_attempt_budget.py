"""通过真实 PostgreSQL 表锁与 ARQ timeout 验证 Run 总尝试预算。"""

import asyncio
import json
import os
import uuid
from pathlib import Path

import asyncpg
import httpx
import pytest

from yuxi.services.run_queue_service import close_queue_clients, get_arq_pool, list_recent_run_stream_events


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_durable_attempt_budget_and_execution_timeout():
    """准备阶段超时跨新 job 有界；执行中超时不重放，正常请求和用户取消保留原语义。"""
    if os.getenv("RUN_ATTEMPT_BUDGET_E2E") != "1":
        pytest.skip("通过 run_attempt_budget.sh 在一次性环境运行")
    state = Path("/state")
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    async with httpx.AsyncClient(base_url="http://api:5050", timeout=30) as api:
        try:
            assert await conn.fetchval("SELECT count(*) FROM users") == 0, "仅允许一次性空库"
            password = uuid.uuid4().hex
            response = await api.post("/api/auth/initialize", json={"uid": "retry_admin", "password": password})
            assert response.status_code == 200
            headers = {"Authorization": "Bearer " + response.json()["access_token"]}

            async def post(path, body):
                """调用真实已认证 HTTP 入口，失败时只输出状态。"""
                response = await api.post(path, headers=headers, json=body)
                assert response.status_code == 200, (path, response.status_code)
                return response.json()

            await post(
                "/api/system/model-providers",
                {
                    "provider_id": "retry-model",
                    "display_name": "重试测试模型",
                    "provider_type": "openai",
                    "base_url": "http://replay:8777/v1",
                    "api_key": "synthetic-test-only",
                    "capabilities": ["chat"],
                    "enabled_models": [
                        {"id": "retry-test", "display_name": "retry-test", "type": "chat", "source": "manual"}
                    ],
                    "is_enabled": True,
                },
            )
            await post(
                "/api/system/mcp-servers",
                {
                    "slug": "retry-effect",
                    "name": "retry-effect",
                    "transport": "streamable_http",
                    "url": "http://replay:8778/mcp",
                },
            )
            response = await api.put(
                "/api/system/mcp-servers/retry-effect/status", headers=headers, json={"enabled": True}
            )
            assert response.status_code == 200
            await post(
                "/api/agent",
                {
                    "name": "retry-agent",
                    "slug": "retry-agent",
                    "backend_id": "ChatbotAgent",
                    "config_json": {
                        "context": {
                            "model": "retry-model:retry-test",
                            "system_prompt": "确定性测试协议",
                            "tools": [],
                            "knowledges": [],
                            "mcps": ["retry-effect"],
                            "skills": [],
                            "subagents": [],
                        }
                    },
                },
            )

            async def submit(marker):
                """为每个场景创建独立线程与请求。"""
                thread = await post("/api/chat/thread", {"agent_id": "retry-agent", "title": "重试测试 " + marker})
                run = await post(
                    "/api/agent/runs",
                    {
                        "agent_slug": "retry-agent",
                        "thread_id": thread["id"],
                        "query": marker,
                        "meta": {"request_id": str(uuid.uuid4())},
                    },
                )
                return run["run_id"]

            async def settled(run_id):
                """回读持久终态和清理完成，不能仅根据队列或 HTTP 成功判定。"""
                for _ in range(300):
                    row = await conn.fetchrow(
                        "SELECT status,error_type,error_message,output_message_id,runtime_cleanup_pending "
                        "FROM agent_runs WHERE id=$1",
                        run_id,
                    )
                    if row["status"] in {"completed", "failed", "cancelled"} and not row["runtime_cleanup_pending"]:
                        return row
                    await asyncio.sleep(0.1)
                pytest.fail("Run 未收敛: " + str(dict(row)))

            def effects(marker):
                """独立读取 MCP 服务端文件，统计真正发生的动作。"""
                file = state / "effects.jsonl"
                return (
                    [row for row in map(json.loads, file.read_text().splitlines()) if row["marker"] == marker]
                    if file.exists()
                    else []
                )

            for _ in range(300):
                if (state / "worker-ready").exists():
                    break
                await asyncio.sleep(0.1)
            assert (state / "worker-ready").exists()
            # 请求先入库，再阻塞真实用户读取；启动完成的 worker 由屏障开始领取。
            preparing = await submit("normal")
            blocker = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
            try:
                await blocker.execute("BEGIN")
                await blocker.execute("LOCK TABLE users IN ACCESS EXCLUSIVE MODE")
                (state / "start-worker").touch()
                for _ in range(300):
                    first = await conn.fetchrow(
                        "SELECT outcome FROM agent_run_attempts WHERE run_id=$1 AND attempt_no=1", preparing
                    )
                    ready = await conn.fetchval(
                        "SELECT status='pending' AND NOT runtime_cleanup_pending FROM agent_runs WHERE id=$1", preparing
                    )
                    if first and first["outcome"] == "retry_released" and ready:
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("首次真实准备超时未释放并完成清理")
                # 使用全新 job ID 保证 ARQ job_try 从 1 开始，不修改持久 attempt 或 worker。
                pool = await get_arq_pool()
                job = await pool.enqueue_job("process_agent_run", preparing, _job_id="fresh-" + uuid.uuid4().hex)
                for _ in range(300):
                    attempts = await conn.fetch(
                        "SELECT attempt_no,outcome,error_type FROM agent_run_attempts "
                        "WHERE run_id=$1 ORDER BY attempt_no",
                        preparing,
                    )
                    if len(attempts) >= 2 and attempts[1]["outcome"] is not None:
                        break
                    await asyncio.sleep(0.1)
                assert [a["outcome"] for a in attempts] == ["retry_released", "failed"], [dict(a) for a in attempts]
                assert attempts[1]["error_type"] == "run_retry_exhausted"
                await job.result(timeout=30)
                assert (await job.result_info()).job_try == 1
            finally:
                await blocker.close()
            ready = await api.get("/api/system/ready")
            assert ready.status_code == 200 and ready.json()["degraded"] is False
            row = await settled(preparing)
            assert row["status"] == "failed" and row["error_type"] == "run_retry_exhausted"
            assert effects("normal") == []
            response = await api.get(f"/api/agent/runs/{preparing}/events", headers=headers)
            assert response.status_code == 200 and "run_retry_exhausted" in response.text
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            errors = [
                e["payload"]
                for e in events
                if e.get("payload", {}).get("chunk", {}).get("error_type") == "run_retry_exhausted"
            ]
            assert errors and all(e["chunk"]["retryable"] is False for e in errors)
            duplicate = await pool.enqueue_job("process_agent_run", preparing, _job_id="terminal-" + uuid.uuid4().hex)
            await duplicate.result(timeout=30)
            assert await conn.fetchval("SELECT count(*) FROM agent_run_attempts WHERE run_id=$1", preparing) == 2

            (state / "release-normal").touch()
            normal = await submit("normal")
            row = await settled(normal)
            assert row["status"] == "completed" and len(effects("normal")) == 1
            output = await conn.fetchrow("SELECT run_id,content FROM messages WHERE id=$1", row["output_message_id"])
            assert output["run_id"] == normal and output["content"] == "DONE:normal"

            stopped = await submit("stopped")
            row = await settled(stopped)
            assert len(effects("stopped")) == 1
            assert row["status"] == "failed" and row["error_type"] == "execution_outcome_unknown"
            job = await pool.enqueue_job("process_agent_run", stopped, _job_id="duplicate-" + uuid.uuid4().hex)
            await job.result(timeout=30)
            assert len(effects("stopped")) == 1
            attempts = await conn.fetch(
                "SELECT outcome,error_type,finished_at FROM agent_run_attempts WHERE run_id=$1", stopped
            )
            assert len(attempts) == 1 and attempts[0]["outcome"] == "failed"
            assert attempts[0]["error_type"] == "execution_outcome_unknown" and attempts[0]["finished_at"] is not None
            events = await list_recent_run_stream_events(stopped, limit=100)
            assert "execution_outcome_unknown" in json.dumps(events)

            cancelled = await submit("stopped")
            for _ in range(40):
                if len(effects("stopped")) == 2:
                    break
                await asyncio.sleep(0.05)
            assert len(effects("stopped")) == 2
            await post(f"/api/agent/runs/{cancelled}/cancel", {})
            row = await settled(cancelled)
            assert row["status"] == "cancelled", dict(row)
            attempts = await conn.fetch("SELECT outcome FROM agent_run_attempts WHERE run_id=$1", cancelled)
            assert len(attempts) == 1 and attempts[0]["outcome"] == "cancelled"
        finally:
            (state / "release-stopped").touch()
            await close_queue_clients()
            await conn.close()
