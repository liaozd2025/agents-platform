"""通过真实 HTTP、worker、PI 与文件验证阶段交接及指定来源。"""

from __future__ import annotations

import asyncio
import json
import uuid

import asyncpg
import pytest
import pytest_asyncio

from e2e_helpers import cancel_run, delete_agent, iter_sse, postgres_dsn, wait_for_run
from test.live_api_cleanup import (
    delete_test_conversation_resources,
    make_test_conversation_metadata,
    make_test_conversation_title,
    validate_test_runs_terminal,
)
from yuxi.workspace.workdir import Workdir

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


@pytest.fixture(scope="module", autouse=True)
def cleanup_e2e_test_resources():
    """本模块精确清理自己创建的资源，不扫描其他测试会话。"""
    yield


@pytest_asyncio.fixture
async def delegation_environment(e2e_client, e2e_headers):
    """从 API 建立独立 Agent、模型和项目，结束后清理本次事实。"""
    upstream = "http://sandbox-provisioner:8765"
    client, headers = e2e_client, e2e_headers
    nonce = uuid.uuid4().hex
    slug = f"pytest-pi-{nonce[:16]}"
    me = await client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    context = {
        "nonce": nonce,
        "uid": str(me.json()["uid"]),
        "slug": slug,
        "model": f"{slug}:pi-delegation-controlled",
        "runs": [],
    }
    connection = await asyncpg.connect(postgres_dsn())
    try:
        response = await client.post(
            "/api/system/model-providers",
            headers=headers,
            json={
                "provider_id": slug,
                "display_name": "PI delegation E2E",
                "provider_type": "openai",
                "base_url": upstream.rstrip("/") + "/v1",
                "api_key": "pi-http-replay-key",
                "capabilities": ["chat"],
                "enabled_models": [
                    {"id": "pi-delegation-controlled", "display_name": "controlled", "type": "chat", "source": "manual"}
                ],
                "is_enabled": True,
            },
        )
        assert response.status_code == 200, response.text
        response = await client.post(
            "/api/agent",
            headers=headers,
            json={
                "name": "PI delegation E2E",
                "slug": slug,
                "backend_id": "ChatbotAgent",
                "config_json": {
                    "context": {
                        "model": context["model"],
                        "system_prompt": "按用户要求完成 PI 阶段并核对原文。",
                        "tools": [],
                        "skills": [],
                        "knowledges": [],
                        "mcps": [],
                        "subagents": [],
                    }
                },
                "share_config": {
                    "version": 2,
                    "read_scope": {"access_level": "user", "department_ids": [], "user_uids": [context["uid"]]},
                    "manage_scope": None,
                },
            },
        )
        assert response.status_code == 200, response.text
        response = await client.post(
            "/api/chat/thread",
            headers=headers,
            json={
                "agent_id": slug,
                "title": make_test_conversation_title("pi-delegation"),
                "metadata": make_test_conversation_metadata("pi-delegation", e2e=True),
            },
        )
        assert response.status_code == 200, response.text
        context.update(response.json())
        context["db"] = connection
        # worker 的模型配置本地缓存为 5 秒；准备完成后再验证任务执行。
        await asyncio.sleep(6)
        yield context
    finally:
        for run_id in context["runs"]:
            await cancel_run(client, headers, run_id)
            await wait_for_run(client, headers, run_id)
        if context.get("project_id"):
            threads = {
                row["thread_id"]
                for row in await connection.fetch(
                    "SELECT thread_id FROM conversations WHERE project_id=$1 AND uid=$2",
                    context["project_id"],
                    context["uid"],
                )
            }
            await validate_test_runs_terminal(threads)
            response = await client.delete(f"/api/chat/thread/{context['id']}", headers=headers)
            assert response.status_code in {200, 404}, response.text
            await delete_test_conversation_resources(
                {(context["uid"], context["workdir_path"]): {context["project_id"]}}, threads, {context["project_id"]}
            )
        await delete_agent(client, headers, slug)
        await client.delete(f"/api/system/model-providers/{slug}", headers=headers)
        await connection.close()


@pytest.mark.parametrize("defective", [False, True])
async def test_edit_check_then_correct_preserves_explicit_history(
    e2e_client, e2e_headers, delegation_environment, defective
):
    """A 编辑、B 独立核验、C 修正 A；父读实际文件且不重新委派核验。"""
    client, headers, ctx = e2e_client, e2e_headers, delegation_environment
    sources = []
    for stage in "ABC":
        response = await client.post(
            "/api/agent/runs",
            headers=headers,
            json={
                "agent_slug": ctx["slug"],
                "thread_id": ctx["id"],
                "model_spec": ctx["model"],
                "tool_approval_mode": "always_trust",
                "query": f"PI_DELEGATION:{ctx['nonce']}:{stage}"
                + (" MISSING_EVIDENCE" if defective and stage == "A" else ""),
                "meta": {"request_id": f"pytest-pi-{uuid.uuid4().hex}"},
            },
        )
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
        ctx["runs"].append(run_id)
        async with asyncio.timeout(120):
            async for event, _payload in iter_sse(client, headers, run_id):
                if event == "end":
                    break
        run = await wait_for_run(client, headers, run_id)
        assert run["status"] == "completed", run
        assert not run["runtime_cleanup_pending"]
        rows = await ctx["db"].fetch(
            "SELECT r.id,r.status,m.extra_metadata,a.runtime_manifest FROM agent_runs r "
            "JOIN messages m ON m.id=r.output_message_id "
            "JOIN agent_run_attempts a ON a.run_id=r.id AND a.final_acked_at IS NOT NULL "
            "WHERE r.created_by_run_id=$1 AND r.run_type='sandbox' ORDER BY r.created_at",
            run_id,
        )
        assert len(rows) == (2 if defective and stage == "A" else 1)
        assert all(row["status"] == "completed" for row in rows)
        child = rows[-1]
        sources.append(child["id"])
        manifest = json.loads(child["runtime_manifest"])
        metadata = json.loads(child["extra_metadata"])["pi"]
        expected = sources[0] if stage == "C" else (rows[0]["id"] if len(rows) == 2 else None)
        assert metadata["source_run_id"] == expected
        assert (manifest["context"]["session_source"] or {}).get("run_id") == expected
        workdir = Workdir.open_existing(ctx["uid"], ctx["workdir_path"])
        if len(rows) == 2:
            initial = json.loads(rows[0]["extra_metadata"])["pi"]
            original = workdir.read_file(f"/outputs/{initial['output_subdir']}/verification.txt", 1024)
            assert b"MISSING_VALUE" in original
        content = workdir.read_file(f"/outputs/{metadata['output_subdir']}/verification.txt", 1024)
        assert content.decode() == f"{stage}: verified\n{ctx['nonce']}\n"


async def test_user_supplement_resumes_incomplete_pi_stage(e2e_client, e2e_headers, delegation_environment):
    """缺资料时父等待用户，补值后续接原 PI 并回读真实成品。"""
    client, headers, ctx = e2e_client, e2e_headers, delegation_environment
    supplied = "测试科室-" + uuid.uuid4().hex
    source = interrupted_run = None
    for resume in (False, True):
        payload = {
            "agent_slug": ctx["slug"],
            "thread_id": ctx["id"],
            "model_spec": ctx["model"],
            "tool_approval_mode": "always_trust",
            "meta": {"request_id": f"pytest-pi-{uuid.uuid4().hex}"},
        }
        if resume:
            payload.update(resume={"answer": supplied}, created_by_run_id=interrupted_run)
        else:
            payload["query"] = f"PI_DELEGATION:{ctx['nonce']}:S 申请科室缺失，请先询问用户再填写。"
        response = await client.post("/api/agent/runs", headers=headers, json=payload)
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
        ctx["runs"].append(run_id)
        async with asyncio.timeout(120):
            async for event, _payload in iter_sse(client, headers, run_id):
                if event == "end":
                    break
        run = await wait_for_run(client, headers, run_id)
        assert run["status"] == ("completed" if resume else "interrupted"), run
        assert not run["runtime_cleanup_pending"]
        rows = await ctx["db"].fetch(
            "SELECT r.id,m.extra_metadata FROM agent_runs r JOIN messages m ON m.id=r.output_message_id "
            "WHERE r.created_by_run_id=$1 AND r.run_type='sandbox' AND r.status='completed'",
            run_id,
        )
        assert len(rows) == 1
        metadata = json.loads(rows[0]["extra_metadata"])["pi"]
        assert metadata["source_run_id"] == source
        if resume:
            workdir = Workdir.open_existing(ctx["uid"], ctx["workdir_path"])
            content = workdir.read_file(f"/outputs/{metadata['output_subdir']}/verification.txt", 1024)
            assert content.decode() == f"申请科室：{supplied}\n{ctx['nonce']}\n"
        else:
            source, interrupted_run = rows[0]["id"], run_id
