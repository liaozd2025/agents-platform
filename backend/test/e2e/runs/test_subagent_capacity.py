"""由 run_subagent_capacity.sh 在一次性真实 HTTP/worker/PG/SSE 环境中验证父子运行容量与终态。"""

import asyncio
import json
import os
import uuid

import asyncpg
import httpx
import pytest
from yuxi.workspace.paths import user_workspace_dir


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_four_waiting_parents_and_children_complete():
    """四父全部占位后委派，父子最终消息各自绑定唯一 Run 与 Attempt。"""
    if os.getenv("SUBAGENT_CAPACITY_E2E") != "1":
        pytest.skip("通过 run_subagent_capacity.sh 使用一次性测试环境")
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

            me = await api.get("/api/auth/me")
            uid = str(me.json()["uid"])
            for wait_tool in ("task", "async"):
                parents = []
                artifacts = {}
                threads = []
                for index in range(4):
                    thread = await post("/api/chat/thread", {"agent_id": "failure-main", "title": f"capacity-{index}"})
                    threads.append(thread)
                    path = user_workspace_dir(uid) / thread["workdir_path"] / "result.txt"
                    submitted = await post(
                        "/api/agent/runs",
                        {
                            "agent_slug": "failure-main",
                            "thread_id": thread["id"],
                            "query": json.dumps(
                                {
                                    "case": "capacity",
                                    "wait": wait_tool,
                                    "path": f"/home/gem/user-data/{thread['workdir_path']}/result.txt",
                                    "content": f"artifact-{index}",
                                }
                            ),
                            "tool_approval_mode": "always_trust",
                            "meta": {"request_id": str(uuid.uuid4())},
                        },
                    )
                    parents.append(submitted["run_id"])
                    artifacts[submitted["run_id"]] = (path, f"artifact-{index}")
                started = asyncio.get_running_loop().time()
                for _ in range(900):
                    rows = await conn.fetch(
                        "SELECT r.id,r.created_by_run_id,r.status,r.error_type,r.error_message,"
                        "r.runtime_cleanup_pending,"
                        "r.output_message_id,m.run_id AS message_run_id,m.content "
                        "FROM agent_runs r LEFT JOIN messages m ON m.id=r.output_message_id "
                        "WHERE r.id=ANY($1::text[]) OR r.created_by_run_id=ANY($1::text[])",
                        parents,
                    )
                    if len(rows) == 8 and all(
                        r["status"] == "completed" and not r["runtime_cleanup_pending"] for r in rows
                    ):
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail(str([dict(r) for r in rows]))
                for row in rows:
                    parent_id = row["id"] if row["id"] in parents else row["created_by_run_id"]
                    path, content = artifacts[parent_id]
                    kind = "parent" if row["id"] in parents else "child"
                    expected = f"DONE:{kind}:{content}"
                    assert path.read_text() == content
                    assert row["message_run_id"] == row["id"] and row["content"] == expected
                    attempts = await conn.fetch("SELECT outcome FROM agent_run_attempts WHERE run_id=$1", row["id"])
                    assert len(attempts) == 1 and attempts[0]["outcome"] == "completed"
                assert {r["created_by_run_id"] for r in rows if r["id"] not in parents} == set(parents)
                print(
                    f"{wait_tool}: four parents and four children completed in "
                    f"{asyncio.get_running_loop().time() - started:.3f}s"
                )
            thread = threads[0]
            cancel_path = user_workspace_dir(uid) / thread["workdir_path"] / "cancelled.txt"
            cancelled = await post(
                "/api/agent/runs",
                {
                    "agent_slug": "failure-main",
                    "thread_id": thread["id"],
                    "query": json.dumps(
                        {
                            "case": "cancel",
                            "path": f"/home/gem/user-data/{thread['workdir_path']}/cancelled.txt",
                            "content": "must-not-exist",
                        }
                    ),
                    "tool_approval_mode": "always_trust",
                    "meta": {"request_id": str(uuid.uuid4())},
                },
            )
            async with httpx.AsyncClient(base_url="http://replay:8777") as model:
                for _ in range(200):
                    if (await model.get("/")).json().get("cancel:child:before") == 1:
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("取消样本没有进入子执行")
                next_path = user_workspace_dir(uid) / thread["workdir_path"] / "next.txt"
                following = await post(
                    "/api/agent/runs",
                    {
                        "agent_slug": "failure-main",
                        "thread_id": thread["id"],
                        "query": json.dumps(
                            {
                                "case": "following",
                                "path": f"/home/gem/user-data/{thread['workdir_path']}/next.txt",
                                "content": "fifo-next",
                            }
                        ),
                        "tool_approval_mode": "always_trust",
                        "meta": {"request_id": str(uuid.uuid4())},
                    },
                )
                assert following.get("run_id") is None, following
                assert not next_path.exists()
                response = await api.post(f"/api/agent/runs/{cancelled['run_id']}/cancel")
                assert response.status_code == 200, response.text
                for _ in range(200):
                    active = await conn.fetchval(
                        "SELECT count(*) FROM agent_runs WHERE (id=$1 OR created_by_run_id=$1) "
                        "AND (status NOT IN ('completed','failed','cancelled') OR runtime_cleanup_pending)",
                        cancelled["run_id"],
                    )
                    if not active:
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("父子取消后执行树未关闭")
                await model.get("/release")
                queued = await conn.fetchrow(
                    "SELECT status,dispatched_run_id FROM agent_run_requests WHERE request_id=$1",
                    following["request_id"],
                )
                assert queued["status"] == "queued" and queued["dispatched_run_id"] is None
                response = await api.post(
                    f"/api/agent/thread/{thread['id']}/requests/continue", params={"agent_slug": "failure-main"}
                )
                assert response.status_code == 200, response.text
                assert response.json()["request_id"] == following["request_id"]
                for _ in range(600):
                    final = await conn.fetchrow(
                        "SELECT r.id,r.status,m.run_id AS message_run_id,m.content FROM agent_run_requests q "
                        "JOIN agent_runs r ON r.id=q.dispatched_run_id "
                        "LEFT JOIN messages m ON m.id=r.output_message_id "
                        "WHERE q.request_id=$1",
                        following["request_id"],
                    )
                    if final and final["status"] == "completed":
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("显式继续后 FIFO 没有推进")
                assert final["message_run_id"] == final["id"]
                assert final["content"] == "DONE:parent:fifo-next"
                assert next_path.read_text() == "fifo-next"
                assert not cancel_path.exists()
                assert (await model.get("/")).json().get("cancel:parent:after", 0) == 0
                statuses = await conn.fetch(
                    "SELECT status FROM agent_runs WHERE id=$1 OR created_by_run_id=$1", cancelled["run_id"]
                )
                assert len(statuses) == 2 and all(r["status"] == "cancelled" for r in statuses)
        finally:
            await conn.close()
