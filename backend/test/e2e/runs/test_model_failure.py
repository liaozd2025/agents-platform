"""由 run_model_failure.sh 在一次性真实 HTTP/worker/PG/SSE 环境中验证模型失败终态。"""

import asyncio
import json
import os
import uuid

import asyncpg
import httpx
import pytest


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_model_failure_is_not_a_completed_answer():
    """主模型与真实委派子模型均保留失败；正常和暂时失败后的成功作为对照。"""
    if os.getenv("MODEL_FAILURE_E2E") != "1":
        pytest.skip("通过 run_model_failure.sh 使用一次性测试环境")
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    async with httpx.AsyncClient(base_url="http://api:5050", timeout=45) as api:
        try:
            assert await conn.fetchval("SELECT count(*) FROM users") == 0, "仅允许一次性空库"
            response = await api.post(
                "/api/auth/initialize", json={"uid": "model_failure_test", "password": uuid.uuid4().hex}
            )
            assert response.status_code == 200
            api.headers["Authorization"] = "Bearer " + response.json()["access_token"]

            async def post(path, body):
                """调用真实 HTTP，拒绝只用成功状态替代最终事实。"""
                response = await api.post(path, json=body)
                assert response.status_code == 200, (path, response.status_code)
                return response.json()

            await post(
                "/api/system/model-providers",
                {
                    "provider_id": "failure-provider",
                    "display_name": "失败测试模型",
                    "provider_type": "openai",
                    "base_url": "http://replay:8777/v1",
                    "api_key": "synthetic-only",
                    "capabilities": ["chat"],
                    "is_enabled": True,
                    "enabled_models": [
                        {"id": "failure-test", "display_name": "failure-test", "type": "chat", "source": "manual"}
                    ],
                },
            )
            for slug, child in [("failure-child", True), ("failure-main", False)]:
                await post(
                    "/api/agent",
                    {
                        "name": slug,
                        "slug": slug,
                        "backend_id": "SubAgentBackend" if child else "ChatbotAgent",
                        "is_subagent": child,
                        "config_json": {
                            "context": {
                                "model": "failure-provider:failure-test",
                                "model_retry_times": 2,
                                "system_prompt": "本地合成模型测试",
                                "tools": [],
                                "knowledges": [],
                                "skills": [],
                                "mcps": [],
                                "subagents": ["failure-child"] if not child else [],
                            }
                        },
                    },
                )

            async def settled(run_id):
                """等待终态和清理完成，同时保留超时的真实状态。"""
                for _ in range(400):
                    row = await conn.fetchrow("SELECT * FROM agent_runs WHERE id=$1", run_id)
                    if row["status"] in {"completed", "failed", "cancelled"} and not row["runtime_cleanup_pending"]:
                        return row
                    await asyncio.sleep(0.1)
                pytest.fail(f"Run 未收敛: {run_id} {row['status']}")

            mismatches = []
            for kind in ["main", "delegate"]:
                for mode in ["normal", "recover", "exhausted"]:
                    marker = f"{kind}:{mode}"
                    thread = await post("/api/chat/thread", {"agent_id": "failure-main", "title": marker})
                    submitted = await post(
                        "/api/agent/runs",
                        {
                            "agent_slug": "failure-main",
                            "thread_id": thread["id"],
                            "query": marker,
                            "meta": {"request_id": str(uuid.uuid4())},
                        },
                    )
                    parent = await settled(submitted["run_id"])
                    row = parent
                    if kind == "delegate":
                        assert parent["status"] == "completed", (marker, parent["error_type"])
                        children = await conn.fetch(
                            "SELECT id FROM agent_runs WHERE created_by_run_id=$1 AND run_type='subagent'", parent["id"]
                        )
                        assert len(children) == 1, marker
                        row = await settled(children[0]["id"])
                    expected = "failed" if mode == "exhausted" else "completed"
                    if row["status"] != expected:
                        mismatches.append({"case": marker, "expected": expected, "actual": row["status"]})
                        continue
                    assert not row["runtime_cleanup_pending"]
                    attempts = await conn.fetch(
                        "SELECT outcome,error_type,finished_at,cleanup_error FROM agent_run_attempts WHERE run_id=$1",
                        row["id"],
                    )
                    assert len(attempts) == 1 and attempts[0]["outcome"] == expected, marker
                    assert attempts[0]["finished_at"] is not None and attempts[0]["cleanup_error"] is None
                    message = await conn.fetchrow(
                        "SELECT run_id,content,extra_metadata FROM messages WHERE id=$1", row["output_message_id"]
                    )
                    assert message and message["run_id"] == row["id"], marker
                    response = await api.get(f"/api/agent/runs/{row['id']}")
                    assert response.status_code == 200 and response.json()["run"]["status"] == expected
                    response = await api.get(f"/api/agent/runs/{row['id']}/events")
                    assert response.status_code == 200
                    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                    ends = [event for event in events if event.get("payload", {}).get("status") == expected]
                    assert ends, (marker, "缺少权威终态事件")
                    if mode == "exhausted":
                        assert row["error_type"] == "unexpected_error" and row["error_message"]
                        assert attempts[0]["error_type"] == row["error_type"]
                        assert message["content"] == ""
                        assert json.loads(message["extra_metadata"])["is_error"] is True
                        assert "synthetic model rejected request" in response.text
                    else:
                        assert row["error_type"] is None and attempts[0]["error_type"] is None
                        target = "main" if kind == "main" else "child"
                        assert message["content"] == f"DONE:{target}:{mode}"
            async with httpx.AsyncClient() as model:
                counts = (await model.get("http://replay:8777/")).json()
            for kind in ["main", "child"]:
                assert {mode: counts[f"{kind}:{mode}"] for mode in ["normal", "recover", "exhausted"]} == {
                    "normal": 1,
                    "recover": 2,
                    "exhausted": 3,
                }, counts
            assert not mismatches, mismatches
        finally:
            await conn.close()
