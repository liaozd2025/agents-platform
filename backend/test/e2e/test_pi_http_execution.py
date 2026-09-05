"""从真实HTTP请求验证worker、PI runTask、SSE及不可变交付事实。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
import uuid

import asyncpg
import httpx
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

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.slow]
UPSTREAM = "http://sandbox-provisioner:8765"
FILE_SIZE = 9 * 1024 * 1024
FILE_DIGEST = hashlib.sha256(b"\0" * FILE_SIZE).hexdigest()


@pytest.fixture(scope="module", autouse=True)
def cleanup_e2e_test_resources():
    """覆盖全局宽扫fixture，本模块只清理自己记录的精确资源。"""
    yield


@asynccontextmanager
async def database():
    """为一次事实回读创建独立数据库连接。"""
    connection = await asyncpg.connect(postgres_dsn())
    try:
        yield connection
    finally:
        await connection.close()


def decoded(value):
    """统一asyncpg默认JSON返回与已解码对象。"""
    return json.loads(value) if isinstance(value, str) else value


async def wait_cleanup(client, headers, run_id):
    """终态之后继续确认worker已归还运行域清理所有权。"""
    run = await wait_for_run(client, headers, run_id)
    async with asyncio.timeout(45):
        while run.get("runtime_cleanup_pending"):
            await asyncio.sleep(0.2)
            response = await client.get(f"/api/agent/runs/{run_id}", headers=headers)
            assert response.status_code == 200, response.text
            run = response.json()["run"]
    return run


@pytest_asyncio.fixture
async def pi_environment(e2e_client, e2e_headers):
    """通过API创建专属配置和Project；只销毁本fixture创建的身份。"""
    client, headers = e2e_client, e2e_headers
    me = await client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    nonce = uuid.uuid4().hex
    context = {
        "nonce": nonce,
        "uid": str(me.json()["uid"]),
        "provider": f"pytest-pi-{nonce[:16]}",
        "agent": f"pytest-pi-{nonce[:16]}",
        "project": None,
        "thread": None,
        "run": None,
        "stream": None,
    }
    context["model"] = f"{context['provider']}:pi-http-controlled"
    try:
        response = await client.post(
            "/api/system/model-providers",
            headers=headers,
            json={
                "provider_id": context["provider"],
                "display_name": "PI HTTP controlled E2E",
                "provider_type": "openai",
                "base_url": UPSTREAM + "/v1",
                "api_key": "pi-http-replay-key",
                "capabilities": ["chat"],
                "enabled_models": [
                    {
                        "id": "pi-http-controlled",
                        "display_name": "PI HTTP controlled",
                        "type": "chat",
                        "source": "manual",
                    }
                ],
                "is_enabled": True,
            },
        )
        assert response.status_code == 200, response.text
        response = await client.post(
            "/api/agent",
            headers=headers,
            json={
                "name": f"PI HTTP {nonce[:8]}",
                "slug": context["agent"],
                "backend_id": "ChatbotAgent",
                "description": "真实PI交付和取消验收",
                "config_json": {
                    "context": {
                        "model": context["model"],
                        "system_prompt": "将用户完整沙箱任务交给pi_sandbox并报告真实结果。",
                        "tools": [],
                        "knowledges": [],
                        "mcps": [],
                        "skills": [],
                        "preload_skills": [],
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
                "agent_id": context["agent"],
                "title": make_test_conversation_title("pi-http"),
                "metadata": make_test_conversation_metadata("pi-http", e2e=True),
            },
        )
        assert response.status_code == 200, response.text
        context["thread"] = response.json()["id"]
        context["project"] = response.json()["project_id"]
        context["workdir"] = response.json()["workdir_path"]
        assert context["workdir"] == f"projects/{context['project']}"
        yield context
    finally:
        if context.get("queued_request"):
            await client.post(f"/api/agent/requests/{context['queued_request']}/cancel", headers=headers)
            response = await client.get(f"/api/agent/requests/{context['queued_request']}", headers=headers)
            if response.status_code == 200 and (replacement := response.json()["request"].get("dispatched_run_id")):
                await cancel_run(client, headers, replacement)
                await wait_cleanup(client, headers, replacement)
        if context["run"]:
            await cancel_run(client, headers, context["run"])
            await wait_cleanup(client, headers, context["run"])
        if context["stream"]:
            context["stream"].cancel()
            await asyncio.gather(context["stream"], return_exceptions=True)
        if context["project"]:
            async with database() as db:
                rows = await db.fetch(
                    "SELECT thread_id FROM conversations WHERE project_id=$1 AND uid=$2",
                    context["project"],
                    context["uid"],
                )
            threads = {row["thread_id"] for row in rows}
            await validate_test_runs_terminal(threads)
            response = await client.delete(f"/api/chat/thread/{context['thread']}", headers=headers)
            assert response.status_code in {200, 404}, response.text
            await delete_test_conversation_resources(
                {(context["uid"], context["workdir"]): {context["project"]}},
                threads,
                {context["project"]},
            )
        await delete_agent(client, headers, context["agent"])
        response = await client.delete(f"/api/system/model-providers/{context['provider']}", headers=headers)
        assert response.status_code in {200, 404}, response.text


async def start_case(client, headers, context, scenario):
    """从公开Run入口发起请求，同时订阅真实SSE流。"""
    context["request"] = f"pytest-pi-run-{context['nonce']}"
    response = await client.post(
        "/api/agent/runs",
        headers=headers,
        json={
            "agent_slug": context["agent"],
            "thread_id": context["thread"],
            "model_spec": context["model"],
            "tool_approval_mode": "always_trust",
            "meta": {"request_id": context["request"]},
            "query": (
                f"YUXI_PI_HTTP:{context['nonce']}:{scenario} 完成沙箱任务并按指定目录交付。"
                + (f" YUXI_PI_EXPECT_HISTORY:{context['expected_history']}" if context.get("expected_history") else "")
            ),
        },
    )
    assert response.status_code == 200, response.text
    context["run"] = response.json()["run_id"]
    events = []

    async def consume():
        async for event, payload in iter_sse(client, headers, context["run"]):
            events.append((event, payload))
            if event == "end":
                return

    context["stream"] = asyncio.create_task(consume())
    context["events"] = events


async def child_facts(context):
    """只回读当前root的唯一PI child及其最终消息与attempt。"""
    async with database() as db:
        rows = await db.fetch(
            "SELECT r.id,r.status,r.request_id,r.conversation_thread_id,r.runtime_scope_id,r.created_by_run_id,"
            "r.output_message_id,m.run_id AS message_run_id,m.request_id AS message_request_id,"
            "m.content,m.extra_metadata "
            "FROM agent_runs r LEFT JOIN messages m ON m.id=r.output_message_id "
            "WHERE r.created_by_run_id=$1 AND r.run_type='sandbox'",
            context["run"],
        )
        assert len(rows) == 1, f"expected one PI child, got {len(rows)}"
        child = dict(rows[0])
        attempts = await db.fetch("SELECT * FROM agent_run_attempts WHERE run_id=$1 ORDER BY attempt_no", child["id"])
        assert attempts
        child["attempt"] = dict(attempts[-1])
        request = await db.fetchrow(
            "SELECT status,dispatched_run_id FROM agent_run_requests WHERE request_id=$1", context["request"]
        )
        assert request["status"] == "dispatched" and request["dispatched_run_id"] == context["run"]
        return child


def dictionaries(value):
    """遍历现有批量SSE envelope，不假设单项chunk包装层数。"""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from dictionaries(child)


async def test_pi_http_delivers_large_file_with_persisted_lineage(e2e_client, e2e_headers, pi_environment):
    """核对9MiB交付、父子绑定，并在第二轮验证续接与本次用量。"""
    context = pi_environment
    await start_case(e2e_client, e2e_headers, context, "artifact")
    run = await wait_cleanup(e2e_client, e2e_headers, context["run"])
    await asyncio.wait_for(context["stream"], timeout=20)
    assert run["status"] == "completed", run
    result = await e2e_client.get(f"/api/agent/runs/{context['run']}/result", headers=e2e_headers)
    assert result.status_code == 200 and result.json()["output"] == "PI_HTTP_ROOT_OK", result.text
    assert result.json()["request_id"] == context["request"]
    assert result.json()["final_message_id"] == run["output_message_id"]
    async with database() as db:
        root_message = await db.fetchrow(
            "SELECT run_id,request_id,content FROM messages WHERE id=$1", run["output_message_id"]
        )
    assert root_message["run_id"] == context["run"] and root_message["request_id"] == context["request"]
    assert root_message["content"] == "PI_HTTP_ROOT_OK"
    child = await child_facts(context)
    assert child["status"] == "completed" and child["content"] == "PI_HTTP_CHILD_OK", child
    assert child["runtime_scope_id"] == context["thread"]
    assert child["message_run_id"] == child["id"] and child["message_request_id"] == child["request_id"]
    attempt = child["attempt"]
    assert attempt["adapter"] == "local" and attempt["final_acked_at"] and attempt["outcome"] == "completed"
    assert attempt["cleanup_error"] is None and attempt["finished_at"]
    manifest = decoded(attempt["runtime_manifest"])
    assert manifest["model"]["model_id"] == "pi-http-controlled", "golden分支不能替代真实runTask"
    ledger = decoded(attempt["result_events"])
    assert all(item["job_id"] == child["id"] for item in ledger)
    assert {item["type"] for item in ledger} >= {"tool_call", "tool_result", "final"}
    pi = decoded(child["extra_metadata"])["pi"]
    child_result = await e2e_client.get(f"/api/agent/runs/{child['id']}/result", headers=e2e_headers)
    assert child_result.status_code == 200, child_result.text
    assert child_result.json()["final_message_id"] == child["output_message_id"]
    assert child_result.json()["pi"] == pi
    output_dir = "pi-runs/" + hashlib.sha256(f"{child['id']}:{attempt['id']}".encode()).hexdigest()[:24]
    assert pi["output_subdir"] == output_dir
    files = [{"path": "large.bin", "size": FILE_SIZE, "sha256": FILE_DIGEST}]
    assert pi["artifact"]["files"] == files
    relative = f"{context['workdir']}/outputs/{output_dir}"
    response = await e2e_client.get(
        "/api/workspace/download", headers=e2e_headers, params={"path": relative + "/large.bin"}
    )
    assert response.status_code == 200, response.text[:200]
    assert len(response.content) == FILE_SIZE and hashlib.sha256(response.content).hexdigest() == FILE_DIGEST
    response = await e2e_client.get(
        "/api/workspace/download", headers=e2e_headers, params={"path": relative + "/.pi-artifacts.json"}
    )
    assert response.status_code == 200 and response.json() == {"files": files}, response.text
    workdir = Workdir.open_existing(context["uid"], context["workdir"])
    assert len(workdir.list_directory("/node_modules")) == 220
    assert len(workdir.list_directory(f"/outputs/{output_dir}/cache")) == 220
    assert workdir.read_file("/cwd.txt", 4096).decode().strip() == f"/home/gem/user-data/{context['workdir']}"
    state = await e2e_client.get(f"/api/chat/thread/{context['thread']}/state", headers=e2e_headers)
    assert state.status_code == 200, state.text
    assert state.json()["agent_state"]["artifacts"] == [f"/home/gem/user-data/{relative}/large.bin"]
    assert any(
        item["run_id"] == child["id"] and item["child_thread_id"] == child["conversation_thread_id"]
        for item in state.json()["agent_state"]["subagent_runs"]
    )
    tool_calls = {item.get("name") for item in dictionaries(context["events"]) if item.get("type") == "tool_call"}
    assert {"pi_sandbox", "bash", "submit_artifact"} <= tool_calls, context["events"]
    child_events = []
    async with asyncio.timeout(20):
        async for event, payload in iter_sse(e2e_client, e2e_headers, child["id"]):
            child_events.append(payload)
            if event == "end":
                break
    assert child_events and all(
        item["run_id"] == child["id"] and item["thread_id"] == child["conversation_thread_id"] for item in child_events
    )
    child_tool_names = {item.get("name") for item in dictionaries(child_events) if item.get("type") == "tool_call"}
    assert {"bash", "submit_artifact"} <= child_tool_names
    assert sum(event == "end" for event, _payload in context["events"]) == 1
    async with httpx.AsyncClient() as client:
        observed = await client.get(f"{UPSTREAM}/observations/{context['nonce']}")
    assert [item["phase"] for item in observed.json()["records"]] == [
        "parent_delegate",
        "pi_execute",
        "pi_submit",
        "pi_complete",
        "parent_complete",
    ]
    old_session_path = f"/outputs/{output_dir}/{pi['session']['path']}"
    old_session = workdir.read_file(old_session_path, 8 * 1024 * 1024)
    assert hashlib.sha256(old_session).hexdigest() == pi["session"]["sha256"]
    context["expected_history"], context["nonce"] = context["nonce"], uuid.uuid4().hex
    await start_case(e2e_client, e2e_headers, context, "artifact")
    second_run = await wait_cleanup(e2e_client, e2e_headers, context["run"])
    await asyncio.wait_for(context["stream"], timeout=20)
    assert second_run["status"] == "completed", second_run
    second_child = await child_facts(context)
    assert second_child["conversation_thread_id"] == child["conversation_thread_id"]
    assert second_child["id"] != child["id"] and second_child["content"] == "PI_HTTP_CHILD_OK"
    source = decoded(second_child["attempt"]["runtime_manifest"])["context"]["session_source"]
    assert source["run_id"] == child["id"] and source["ref"] == pi["session"]
    assert workdir.read_file(old_session_path, 8 * 1024 * 1024) == old_session
    for completed_child in (child, second_child):
        response = await e2e_client.get(f"/api/agent/runs/{completed_child['id']}", headers=e2e_headers)
        assert response.status_code == 200, response.text
        usage = response.json()["run"]["token_usage"]
        assert usage["complete"] is True and usage["model_call_count"] == 3
        assert usage["total"] == {"input_tokens": 60, "output_tokens": 30, "total_tokens": 90}
    child_state = await e2e_client.get(
        f"/api/chat/thread/{second_child['conversation_thread_id']}/state", headers=e2e_headers
    )
    assert child_state.status_code == 200, child_state.text
    assert child_state.json()["subagent_run"]["run_id"] == second_child["id"]


async def test_pi_http_steer_yields_at_tool_boundary_and_consumes_request_once(e2e_client, e2e_headers, pi_environment):
    """在真实工具运行中引导；ACK后释放工具，队列只执行一次新请求。"""
    context = pi_environment
    await start_case(e2e_client, e2e_headers, context, "steer")
    workdir = Workdir.open_existing(context["uid"], context["workdir"])
    async with asyncio.timeout(120):
        while True:
            try:
                workdir.stat("/steer-started")
                break
            except FileNotFoundError:
                await asyncio.sleep(0.2)
    first = await child_facts(context)
    assert first["status"] == "running"
    replacement_nonce = uuid.uuid4().hex
    request_id = f"pytest-pi-run-{replacement_nonce}"
    context["queued_request"] = request_id
    response = await e2e_client.post(
        "/api/agent/runs",
        headers=e2e_headers,
        json={
            "agent_slug": context["agent"],
            "thread_id": context["thread"],
            "model_spec": context["model"],
            "tool_approval_mode": "always_trust",
            "queue_policy": "steer",
            "meta": {"request_id": request_id},
            "query": f"YUXI_PI_HTTP:{replacement_nonce}:artifact YUXI_PI_EXPECT_HISTORY:{context['nonce']}",
        },
    )
    assert response.status_code == 200 and response.json()["status"] == "queued", response.text
    async with asyncio.timeout(15):
        while True:
            current = await child_facts(context)
            if any(item["type"] == "control_ack" for item in decoded(current["attempt"]["result_events"])):
                break
            await asyncio.sleep(0.1)
    # 命令尚未结束时必须已收到正文与中间输出，不能由最终结果冒充流式反馈。
    async with asyncio.timeout(10):
        while True:
            chunks = list(dictionaries(context["events"]))
            if any(item.get("event") == "tool-progress" for item in chunks) and "PI_HTTP_WORKING" in json.dumps(chunks):
                break
            await asyncio.sleep(0.1)
    with pytest.raises(FileNotFoundError):
        workdir.stat("/steer-finished")
    workdir.create_directory("/", "steer-release")
    original_run = await wait_cleanup(e2e_client, e2e_headers, context["run"])
    await asyncio.wait_for(context["stream"], timeout=20)
    assert original_run["status"] == "completed", original_run
    first = await child_facts(context)
    assert first["status"] == "completed" and decoded(first["extra_metadata"])["pi"]["stop_reason"] == "steer"
    assert workdir.read_file("/steer-finished", 128) == b"finished"
    assert not any(
        item["type"] in {"message_delta", "tool_update"} for item in decoded(first["attempt"]["result_events"])
    )
    async with asyncio.timeout(30):
        while True:
            response = await e2e_client.get(f"/api/agent/requests/{request_id}", headers=e2e_headers)
            assert response.status_code == 200, response.text
            replacement = response.json()["request"].get("dispatched_run_id")
            if replacement:
                break
            await asyncio.sleep(0.1)
    context["run"], context["request"] = replacement, request_id
    replacement_run = await wait_cleanup(e2e_client, e2e_headers, replacement)
    assert replacement_run["status"] == "completed", replacement_run
    assert (await child_facts(context))["content"] == "PI_HTTP_CHILD_OK"
    async with database() as db:
        assert await db.fetchval("SELECT count(*) FROM agent_runs WHERE request_id=$1", request_id) == 1
    async with httpx.AsyncClient() as client:
        observed = await client.get(f"{UPSTREAM}/observations/{context['nonce']}")
        resumed = await client.get(f"{UPSTREAM}/observations/{replacement_nonce}")
    assert [item["phase"] for item in observed.json()["records"]] == ["parent_delegate", "pi_execute"]
    assert [item["phase"] for item in resumed.json()["records"]] == [
        "parent_delegate",
        "pi_execute",
        "pi_submit",
        "pi_complete",
        "parent_complete",
    ]


async def test_pi_http_cancel_stops_writer_and_removes_owned_runtime(e2e_client, e2e_headers, pi_environment):
    """从API取消长写入，回读终态、文件字节与Docker实例归零。"""
    context = pi_environment
    await start_case(e2e_client, e2e_headers, context, "cancel")
    async with asyncio.timeout(120):
        while True:
            response = await e2e_client.get(
                "/api/workspace/download",
                headers=e2e_headers,
                params={"path": context["workdir"] + "/cancel-growth.log"},
            )
            if response.status_code == 200 and len(response.content) >= 15:
                break
            assert response.status_code in {200, 404}, response.text
            await asyncio.sleep(0.2)
    child = await child_facts(context)
    assert child["status"] == "running" and child["attempt"]["final_acked_at"] is None
    instance_id = child["attempt"]["instance_id"]
    provisioner_headers = {"Authorization": "Bearer " + os.environ["SANDBOX_PROVISIONER_TOKEN"]}
    provisioner_url = os.getenv("SANDBOX_PROVISIONER_URL", "http://sandbox-provisioner:8002")
    async with httpx.AsyncClient() as client:
        inventory = await client.get(provisioner_url + "/api/sandboxes", headers=provisioner_headers)
    assert inventory.status_code == 200
    assert any(item["sandbox_id"] == instance_id for item in inventory.json()["sandboxes"])
    await cancel_run(e2e_client, e2e_headers, context["run"])
    run = await wait_cleanup(e2e_client, e2e_headers, context["run"])
    await asyncio.wait_for(context["stream"], timeout=20)
    assert run["status"] == "cancelled" and not run["runtime_cleanup_pending"], run
    child = await child_facts(context)
    assert child["status"] == "cancelled" and child["output_message_id"] is None, child
    assert child["attempt"]["final_acked_at"] is None and child["attempt"]["finished_at"]
    assert child["attempt"]["cleanup_error"] is None
    async with httpx.AsyncClient() as client:
        inventory = await client.get(provisioner_url + "/api/sandboxes", headers=provisioner_headers)
    assert inventory.status_code == 200
    assert all(item["sandbox_id"] != instance_id for item in inventory.json()["sandboxes"]), (
        "all=True Docker inventory仍有原运行域"
    )
    workdir = Workdir.open_existing(context["uid"], context["workdir"])
    assert int(workdir.read_file("/cancel-writer.pid", 128)) > 0
    stopped = workdir.read_file("/cancel-growth.log", 65536)
    await asyncio.sleep(1)
    assert workdir.read_file("/cancel-growth.log", 65536) == stopped
    assert any(
        item.get("type") == "tool_call" and item.get("name") == "bash" for item in dictionaries(context["events"])
    )


async def test_pi_upstream_rejects_non_contract_requests():
    """受控模型必须拒绝错误认证、模型与任务输入。"""
    body = {
        "model": "pi-http-controlled",
        "stream": True,
        "messages": [{"role": "user", "content": "unrecognized task"}],
        "tools": [],
    }
    async with httpx.AsyncClient(base_url=UPSTREAM, timeout=5) as client:
        for headers, payload, error in [
            ({}, body, "invalid_authorization"),
            ({"Authorization": "Bearer pi-http-replay-key"}, {**body, "model": "other"}, "invalid_model_or_stream"),
            ({"Authorization": "Bearer pi-http-replay-key"}, body, "case_marker_missing"),
        ]:
            response = await client.post("/v1/chat/completions", headers=headers, json=payload)
            assert response.status_code == 422 and response.json() == {"error": error}, response.text
