"""通过同目录运行脚本验证 worker 正常停止后的副作用边界。"""

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
async def test_worker_restart_does_not_repeat_unknown_effect():
    """动作已发生但响应未返回时停止 worker，重启及重复投递都不重放。"""
    if os.getenv("RUN_RETRY_E2E") != "1":
        pytest.skip("通过 run_execution_retry.sh 在一次性环境运行")
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

            (state / "release-normal").touch()
            normal = await submit("normal")
            row = await settled(normal)
            assert row["status"] == "completed" and len(effects("normal")) == 1
            output = await conn.fetchrow("SELECT run_id,content FROM messages WHERE id=$1", row["output_message_id"])
            assert output["run_id"] == normal and output["content"] == "DONE:normal"

            stopped = await submit("stopped")
            for _ in range(300):
                if effects("stopped"):
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("尚未观察到工具副作用")
            assert len(effects("stopped")) == 1
            (state / "stop-worker").touch()
            for _ in range(600):
                if (state / "worker-restarted").exists():
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("宿主未完成 worker 停止/重启")
            (state / "release-stopped").touch()
            # 显式再次投递同一 Run 并等待 job 返回，避免仅短暂观察不到重试。
            pool = await get_arq_pool()
            job = await pool.enqueue_job("process_agent_run", stopped, _job_id="duplicate-" + uuid.uuid4().hex)
            await job.result(timeout=30)
            row = await settled(stopped)
            assert len(effects("stopped")) == 1, {"effects": len(effects("stopped")), "run": dict(row)}
            assert row["status"] == "failed" and row["error_type"] == "execution_outcome_unknown"
            assert "不会自动重试" in row["error_message"]
            attempts = await conn.fetch(
                "SELECT outcome,error_type,finished_at FROM agent_run_attempts WHERE run_id=$1", stopped
            )
            assert len(attempts) == 1 and attempts[0]["outcome"] == "failed"
            assert attempts[0]["error_type"] == "execution_outcome_unknown" and attempts[0]["finished_at"] is not None
            assert (
                await conn.fetchval("SELECT count(*) FROM agent_run_requests WHERE dispatched_run_id=$1", stopped) == 1
            )
            events = await list_recent_run_stream_events(stopped, limit=100)
            assert "execution_outcome_unknown" in json.dumps(events)
        finally:
            (state / "release-stopped").touch()
            await close_queue_clients()
            await conn.close()
