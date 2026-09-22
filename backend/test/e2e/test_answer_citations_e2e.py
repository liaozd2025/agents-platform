"""真实知识原文经子 Run、主汇总、恢复与历史接口保存引用的确定性验收。"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from http.server import ThreadingHTTPServer
from threading import Thread

import asyncpg
import httpx
import pytest
import pytest_asyncio
from e2e_helpers import cancel_run, consume_events, delete_agent, postgres_dsn, wait_for_run
from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from test.live_api_cleanup import make_test_conversation_metadata, make_test_conversation_title
from test.support.openai_replay_server import ReplayHandler
from yuxi.knowledge.cache import KNOWLEDGE_BASE_CACHE_KEY_PREFIX
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.models_knowledge import KnowledgeBase, KnowledgeFile

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.slow]


@pytest_asyncio.fixture
async def citation_environment(e2e_client, e2e_headers):
    """仅准备原始资料和显式回放模型，所有答案由真实 worker 工具链生成。"""
    identity = uuid.uuid4().hex
    provider_id = f"ci-citation-{identity[:12]}"
    kb_id = f"pytest-citation-{identity}"
    files = {
        uuid.uuid4().hex: f"资料正文 CITATION_RAW_VALUE:{uuid.uuid4().hex}\n仅原文含有的核查细节。" for _ in range(3)
    }
    server = ThreadingHTTPServer(("0.0.0.0", 0), ReplayHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    engine = create_async_engine(os.environ["POSTGRES_URL"], poolclass=NullPool)
    sessions = async_sessionmaker(engine)
    minio = get_minio_client()
    bucket = minio.KB_BUCKETS["documents"]
    agents, threads, runs = [], [], []
    me = await e2e_client.get("/api/auth/me", headers=e2e_headers)
    assert me.status_code == 200, me.text
    uid = str(me.json()["uid"])
    share = {
        "version": 2,
        "read_scope": {"access_level": "user", "user_uids": [uid], "department_ids": []},
        "manage_scope": None,
    }
    try:
        response = await e2e_client.post(
            "/api/system/model-providers",
            headers=e2e_headers,
            json={
                "provider_id": provider_id,
                "display_name": "Citation deterministic replay",
                "provider_type": "openai",
                "base_url": f"http://api:{server.server_port}/v1",
                "api_key": "ci-replay-key",
                "capabilities": ["chat"],
                "enabled_models": [{"id": "deterministic-chat", "type": "chat", "source": "manual"}],
                "is_enabled": True,
            },
        )
        assert response.status_code == 200, response.text
        # worker 的模型目录本地快照缓存 5 秒；等新 provider 在跨进程读取中生效。
        await asyncio.sleep(5.1)
        async with sessions.begin() as session:
            session.add(KnowledgeBase(kb_id=kb_id, name=kb_id, kb_type="milvus", created_by=uid, share_config=share))
            await session.flush()
            for file_id, raw in files.items():
                object_name = f"{kb_id}/{file_id}.md"
                await minio.aupload_file(bucket_name=bucket, object_name=object_name, data=raw.encode())
                session.add(
                    KnowledgeFile(
                        kb_id=kb_id,
                        file_id=file_id,
                        filename="同名证据.md",
                        created_by=uid,
                        status="parsed",
                        minio_url=f"minio://{bucket}/{object_name}",
                        markdown_file=f"minio://{bucket}/{object_name}",
                    )
                )
        for child in (True, False):
            slug = f"ci-citation-{'child' if child else 'parent'}-{identity[:12]}"
            response = await e2e_client.post(
                "/api/agent",
                headers=e2e_headers,
                json={
                    "name": slug,
                    "slug": slug,
                    "backend_id": "SubAgentBackend" if child else "ChatbotAgent",
                    "is_subagent": child,
                    "description": "确定性引用链路验收",
                    "share_config": share,
                    "config_json": {
                        "context": {
                            "model": f"{provider_id}:deterministic-chat",
                            "system_prompt": "读取实际知识原文后核查，引用实际采用的来源。",
                            "tools": [],
                            "knowledges": [kb_id],
                            "mcps": [],
                            "skills": ["knowledge-base"],
                            "preload_skills": ["knowledge-base"],
                            "subagents": [] if child else [agents[0]],
                        }
                    },
                },
            )
            assert response.status_code == 200, response.text
            agents.append(slug)
        created = await e2e_client.post(
            "/api/chat/thread",
            headers=e2e_headers,
            json={
                "agent_id": agents[1],
                "title": make_test_conversation_title("answer-citations"),
                "metadata": make_test_conversation_metadata("answer-citations", e2e=True),
            },
        )
        assert created.status_code == 200, created.text
        threads.append(str(created.json()["id"]))
        yield {
            "kb_id": kb_id,
            "files": files,
            "child_slug": agents[0],
            "parent_slug": agents[1],
            "thread_id": threads[0],
            "runs": runs,
            "replay_port": server.server_port,
        }
    finally:
        for run_id in runs:
            await cancel_run(e2e_client, e2e_headers, run_id)
        conn = await asyncpg.connect(postgres_dsn())
        try:
            children = await conn.fetch(
                "SELECT conversation.thread_id FROM agent_runs run JOIN conversations conversation "
                "ON conversation.id = run.conversation_id WHERE run.created_by_run_id = ANY($1::text[])",
                runs,
            )
        finally:
            await conn.close()
        for thread_id in {*(str(child["thread_id"]) for child in children), *threads}:
            response = await e2e_client.delete(f"/api/chat/thread/{thread_id}", headers=e2e_headers)
            assert response.status_code in {200, 404}, response.text
        for slug in reversed(agents):
            await delete_agent(e2e_client, e2e_headers, slug)
        response = await e2e_client.delete(f"/api/system/model-providers/{provider_id}", headers=e2e_headers)
        assert response.status_code in {200, 404}, response.text
        async with Redis.from_url(os.environ["REDIS_URL"]) as redis:
            await redis.delete(f"{KNOWLEDGE_BASE_CACHE_KEY_PREFIX}{kb_id}")
        async with sessions.begin() as session:
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))
        await minio.adelete_objects_by_prefix(bucket, f"{kb_id}/")
        await engine.dispose()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        server_thread.join(timeout=5)


async def run_citation_round(client, headers, case, file_ids, mode, *, adopted_file_ids=None):
    """提交真实主请求，必要时回答 interrupt，再回读终态。"""
    token = uuid.uuid4().hex
    payload = {
        "token": token,
        "kb_id": case["kb_id"],
        "file_ids": file_ids,
        "child_slug": case["child_slug"],
        "mode": mode,
        "url": f"https://example.org/citation/{token}",
    }
    if adopted_file_ids is not None:
        payload["adopted_file_ids"] = adopted_file_ids
    response = await client.post(
        "/api/agent/runs",
        headers=headers,
        json={
            "agent_slug": case["parent_slug"],
            "thread_id": case["thread_id"],
            "query": "CITATION_REPLAY:" + json.dumps(payload),
            "meta": {"request_id": f"citation-{token}"},
        },
    )
    assert response.status_code == 200, response.text
    run_id = str(response.json()["run_id"])
    case["runs"].append(run_id)
    await consume_events(client, headers, run_id)
    run = await wait_for_run(client, headers, run_id)
    source_run_id = run_id
    if mode == "resume":
        assert run["status"] == "interrupted", {
            key: run.get(key) for key in ("id", "status", "error_type", "error_message")
        }
        assert run["error_type"] == "ask_user_question_required", {
            key: run.get(key) for key in ("id", "status", "error_type", "error_message")
        }
        conn = await asyncpg.connect(postgres_dsn())
        try:
            for _ in range(100):
                if await conn.fetchval("SELECT runtime_cleanup_pending FROM agent_runs WHERE id = $1", run_id) is False:
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("interrupted Run did not release runtime before resume")
        finally:
            await conn.close()
        response = await client.post(
            "/api/agent/runs",
            headers=headers,
            json={
                "agent_slug": case["parent_slug"],
                "thread_id": case["thread_id"],
                "created_by_run_id": run_id,
                "resume": {"citation-confirm": "简短"},
                "meta": {"request_id": f"citation-resume-{token}"},
            },
        )
        assert response.status_code == 200, response.text
        run_id = str(response.json()["run_id"])
        case["runs"].append(run_id)
        await consume_events(client, headers, run_id)
        run = await wait_for_run(client, headers, run_id)
    assert run["status"] == "completed", {key: run.get(key) for key in ("id", "status", "error_type", "error_message")}
    conn = await asyncpg.connect(postgres_dsn())
    try:
        scopes = await conn.fetch(
            "SELECT child.input_payload AS child_payload, parent.input_payload AS parent_payload "
            "FROM agent_runs child JOIN agent_runs parent ON parent.id=child.created_by_run_id "
            "WHERE parent.id=$1 AND child.run_type='subagent'",
            source_run_id,
        )
    finally:
        await conn.close()
    assert scopes, "引用必须来自本次主 Run 创建的真实子 Run"
    for row in scopes:
        parent_payload = json.loads(row["parent_payload"])
        child_payload = json.loads(row["child_payload"])
        assert parent_payload["knowledge_allowed_kb_ids"] == [case["kb_id"]]
        assert child_payload["runtime"]["knowledge_task_scope"] == [case["kb_id"]]
        assert child_payload["knowledge_task_scope"] == [case["kb_id"]]
        assert child_payload["knowledge_selected_kb_ids"] == [case["kb_id"]]
    return run, source_run_id, payload


async def read_saved_answer(client, headers, run, thread_id):
    """数据库、Run result 与新 HTTP 连接的历史必须返回同一正文和来源。"""
    conn = await asyncpg.connect(postgres_dsn())
    try:
        row = await conn.fetchrow(
            "SELECT id, run_id, content, extra_metadata FROM messages WHERE id = $1",
            run["output_message_id"],
        )
    finally:
        await conn.close()
    assert row and row["run_id"] == run["id"], row
    metadata = json.loads(row["extra_metadata"])
    sources = metadata.get("citation_sources")
    assert sources, metadata
    result = await client.get(f"/api/agent/runs/{run['id']}/result", headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["output"] == row["content"]
    assert result.json()["citation_sources"] == sources
    async with httpx.AsyncClient(base_url=str(client.base_url), timeout=30) as fresh_client:
        history = await fresh_client.get(f"/api/chat/thread/{thread_id}/history", headers=headers)
    assert history.status_code == 200, history.text
    message = next(item for item in history.json()["history"] if item["id"] == row["id"])
    assert message["run_id"] == run["id"]
    assert message["content"] == row["content"]
    assert message["extra_metadata"]["citation_sources"] == sources
    return row["content"], sources, history.json()["history"]


@pytest.mark.parametrize("mode", ["task", "async", "resume"])
async def test_child_citations_survive_history_resume_and_next_run(e2e_client, e2e_headers, citation_environment, mode):
    """子编号冲突、同名文件、仅链接及下一 Run 均不得丢原文或串源。"""
    case = citation_environment
    file_ids = list(case["files"])
    for selected in (file_ids[:2], file_ids[2:]):
        run, source_run_id, payload = await run_citation_round(e2e_client, e2e_headers, case, selected, mode)
        content, sources, history = await read_saved_answer(e2e_client, e2e_headers, run, case["thread_id"])
        by_source = {source["source"]: source for source in sources}
        expected_sources = {f"kb://{case['kb_id']}/{file_id}" for file_id in selected}
        assert set(by_source) == expected_sources, sources
        assert content.count("<cite ") == len(selected) + 1
        for file_id in selected:
            raw = case["files"][file_id]
            source = by_source[f"kb://{case['kb_id']}/{file_id}"]
            assert source["kb_id"] == case["kb_id"] and source["file_id"] == file_id
            assert source["title"]
            excerpts = "\n".join(item["text"] for item in source["excerpts"])
            assert all(line in excerpts for line in raw.splitlines()), excerpts
            assert "核查摘要" not in excerpts and "主助手汇总" not in excerpts
            assert "仅原文含有的核查细节" not in content
        # 未取得网页原文时只保留正文链接，前端从安全 URL 标识显示缺失提示。
        assert f'<cite source="{payload["url"]}" type="url">1</cite>' in content
        conn = await asyncpg.connect(postgres_dsn())
        try:
            children = await conn.fetch(
                "SELECT run.id, run.status, run.output_message_id, conversation.thread_id FROM agent_runs run "
                "JOIN conversations conversation ON conversation.id = run.conversation_id "
                "WHERE run.created_by_run_id = $1 AND run.run_type = 'subagent'",
                source_run_id,
            )
            assert len(children) == len(selected), children
            for child in children:
                assert child["status"] == "completed", dict(child)
                assert (
                    await conn.fetchval(
                        "SELECT count(*) FROM messages WHERE run_id = $1 AND message_type = 'tool_audit' "
                        "AND operation_id = $2 AND execution_status = 'completed'",
                        child["id"],
                        f"call-citation-{payload['token']}-open",
                    )
                    == 1
                )
                child_content, child_sources, _ = await read_saved_answer(
                    e2e_client, e2e_headers, dict(child), child["thread_id"]
                )
                assert ">1</cite>" in child_content
                assert len(child_sources) == 1 and child_sources[0]["source"] in expected_sources
        finally:
            await conn.close()
        if mode == "task":
            calls = [
                call
                for message in history
                if message.get("run_id") == source_run_id
                for call in message.get("tool_calls", [])
                if call["name"] == "task"
            ]
            assert len(calls) == len(selected), calls
            for call in calls:
                result = call["tool_call_result"]
                child_sources = (result.get("artifact") or {}).get("citation_sources") or (
                    call.get("subagent_run") or {}
                ).get("citation_sources")
                assert child_sources and child_sources[0]["source"] in expected_sources, call


async def test_parent_only_cites_adopted_child_evidence(e2e_client, e2e_headers, citation_environment):
    """两个子任务均有真实证据时，主回复只显示实际采用的 A 来源。"""
    case = citation_environment
    file_ids = list(case["files"])[:2]
    run, source_run_id, _ = await run_citation_round(
        e2e_client, e2e_headers, case, file_ids, "task", adopted_file_ids=file_ids[:1]
    )
    content, sources, _ = await read_saved_answer(e2e_client, e2e_headers, run, case["thread_id"])
    source_a, source_b = (f"kb://{case['kb_id']}/{file_id}" for file_id in file_ids)
    assert f'<cite source="{source_a}" type="file">1</cite>' in content
    assert source_b not in content
    assert content.count('type="file"') == 1
    assert source_a in {source["source"] for source in sources}
    conn = await asyncpg.connect(postgres_dsn())
    try:
        children = await conn.fetch(
            "SELECT run.id, run.status, run.output_message_id, conversation.thread_id FROM agent_runs run "
            "JOIN conversations conversation ON conversation.id = run.conversation_id "
            "WHERE run.created_by_run_id = $1 AND run.run_type = 'subagent'",
            source_run_id,
        )
    finally:
        await conn.close()
    assert len(children) == 2, children
    child_sources = set()
    for child in children:
        assert child["status"] == "completed", dict(child)
        _, evidence, _ = await read_saved_answer(e2e_client, e2e_headers, dict(child), child["thread_id"])
        child_sources.update(source["source"] for source in evidence)
    assert child_sources == {source_a, source_b}


async def test_citation_source_document_requires_resource_permission(e2e_client, e2e_headers, citation_environment):
    """持有来源 ID 的其他登录用户仍不能查看未共享原文或下载全文。"""
    case = citation_environment
    departments = await e2e_client.get("/api/departments", headers=e2e_headers)
    assert departments.status_code == 200 and departments.json(), departments.text
    password = f"Pw!{uuid.uuid4().hex}"
    created = await e2e_client.post(
        "/api/auth/users",
        headers=e2e_headers,
        json={
            "username": f"pytest_cite_{uuid.uuid4().hex[:8]}",
            "password": password,
            "department_id": departments.json()[0]["id"],
        },
    )
    assert created.status_code == 200, created.text
    user = created.json()
    try:
        login = await e2e_client.post("/api/auth/token", data={"username": user["uid"], "password": password})
        assert login.status_code == 200
        other_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        me = await e2e_client.get("/api/auth/me", headers=other_headers)
        assert "knowledge_base:read" in me.json()["effective_permissions"], "负向用例必须到达具体知识库权限边界"
        file_id, raw = next(iter(case["files"].items()))
        for endpoint in ("file", "download"):
            params = {"kb_id": case["kb_id"], "file_id": file_id}
            allowed = await e2e_client.get(f"/api/workspace/knowledge/{endpoint}", headers=e2e_headers, params=params)
            assert allowed.status_code == 200, allowed.text
            assert raw == (allowed.json()["content"] if endpoint == "file" else allowed.text)
            denied = await e2e_client.get(f"/api/workspace/knowledge/{endpoint}", headers=other_headers, params=params)
            assert denied.status_code == 404, denied.text
            assert raw not in denied.text
    finally:
        deleted = await e2e_client.delete(f"/api/auth/users/{user['id']}", headers=e2e_headers)
        assert deleted.status_code in {200, 404}, deleted.text


async def test_citation_replay_rejects_missing_original(citation_environment):
    """回放必须拒绝没有原文的工具结果，避免缺陷被固定最终答案掩盖。"""
    case = citation_environment
    token = uuid.uuid4().hex
    payload = {"token": token, "child": True, "kb_id": case["kb_id"], "file_ids": [next(iter(case["files"]))]}
    messages = [{"role": "user", "content": "CITATION_REPLAY:" + json.dumps(payload)}]
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{case['replay_port']}") as client:
        for tool_content in (
            "只有智能体摘要",
            json.dumps({"kb_id": case["kb_id"], "file_id": payload["file_ids"][0], "content": "摘要"}),
        ):
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer ci-replay-key"},
                json={
                    "model": "deterministic-chat",
                    "stream": True,
                    "messages": [
                        *messages,
                        {"role": "tool", "tool_call_id": f"call-citation-{token}-open", "content": tool_content},
                    ],
                },
            )
            assert response.status_code == 422, response.text
            assert response.json()["error"].startswith("citation_contract:"), response.text
