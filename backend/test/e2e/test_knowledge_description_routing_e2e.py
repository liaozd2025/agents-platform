"""真实 HTTP、worker、检索连接器和 PostgreSQL 验证选库装配，不评估模型语义准确率。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from http.server import ThreadingHTTPServer
from threading import Thread

import asyncpg
import pytest
import pytest_asyncio
from e2e_helpers import cancel_run, consume_events, delete_agent, postgres_dsn, wait_for_run
from redis.asyncio import Redis
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from test.live_api_cleanup import make_test_conversation_metadata, make_test_conversation_title
from test.support.openai_replay_server import ReplayHandler, selection_completion
from yuxi.knowledge.cache import KNOWLEDGE_BASE_CACHE_KEY_PREFIX
from yuxi.storage.postgres.models_knowledge import KnowledgeBase

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.slow]


class RoutingReplayHandler(ReplayHandler):
    """只在外部模型与外部内容协议边界回放，保留真实应用全部执行层。"""

    def do_POST(self):  # noqa: N802
        """记录目录、实际检索请求和最终模型输入，提供受控响应。"""
        request = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))))
        state = self.server.routing_state
        if "/datasets/" in self.path:
            kb_id = self.path.split("/datasets/", 1)[1].split("/", 1)[0]
            state["retrievals"].append(kb_id)
            if state["route"].get("retrieval_error"):
                self._write_json(503, {"error": "test_content_unavailable"})
                return
            records = (
                []
                if state["route"].get("empty")
                else [
                    {
                        "score": 0.99,
                        "segment": {
                            "content": "EVIDENCE_" + kb_id,
                            "id": "segment-1",
                            "document": {"id": "file-1", "name": "受控资料"},
                        },
                    }
                ]
            )
            self._write_json(200, {"records": records})
            return
        if request.get("stream") is not True:
            routing = json.loads(request["messages"][-1]["content"])
            state["selections"].append(routing)
            route = state["route"]
            selection = {
                "task_relation": route.get("relation", "continue"),
                "explicit_scope": route.get("scope"),
                "requested_kb_ids": [],
                "clarification": route.get("clarification"),
                "assessments": [
                    {
                        "kb_id": item["kb_id"],
                        "status": "uncertain"
                        if route.get("clarification")
                        else "select"
                        if item["description"] in route.get("descriptions", [])
                        else "skip",
                        "reason": "人工固定测试判定，用于验证执行边界",
                    }
                    for item in routing["catalog"]
                ],
            }
            if route.get("invalid"):
                selection["assessments"].append({"kb_id": "not-authorized", "status": "select", "reason": "非法ID"})
            self._write_json(200, selection_completion(request, selection))
            return
        state["model_inputs"].append(request["messages"])
        user = next(message for message in reversed(request["messages"]) if message["role"] == "user")
        evidence = re.findall(r"EVIDENCE_[a-zA-Z0-9_-]+", str(user["content"]))
        answer = "已核对 " + " ".join(evidence) if evidence else "本轮没有知识库内容依据"
        tool = state["route"].get("tool")
        finish_reason = "stop"
        delta = {"role": "assistant", "content": answer}
        if tool:
            results = [
                item
                for item in request["messages"]
                if item.get("role") == "tool" and item.get("tool_call_id") == state["tool_call_id"]
            ]
            if results:
                state["tool_results"] = results
                delta = {"role": "assistant", "content": "工具核验：" + str(results[-1]["content"])}
            else:
                names = {item["function"]["name"] for item in request.get("tools", [])}
                if tool["name"] not in names:
                    self._write_json(422, {"error": "expected_knowledge_tool_missing"})
                    return
                callback = state.pop("before_tool", None)
                if callback:
                    callback()
                delta = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": state["tool_call_id"],
                            "type": "function",
                            "function": {"name": tool["name"], "arguments": json.dumps(tool["arguments"])},
                        }
                    ],
                }
                finish_reason = "tool_calls"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        response_id = "routing-answer-" + uuid.uuid4().hex
        for delta, finish in [(delta, None), ({}, finish_reason)]:
            payload = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": 1,
                "model": request["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


@pytest_asyncio.fixture
async def routing_environment(e2e_client, e2e_headers):
    """每例自建模型、私有库和智能体，清理自己的完整资源。"""
    identity = uuid.uuid4().hex[:12]
    provider_id = f"ci-routing-{identity}"
    slug = f"ci-routing-agent-{identity}"
    kb_ids = [f"pytest-routing-{identity}-{index}" for index in range(2)]
    state = {"route": {}, "retrievals": [], "selections": [], "model_inputs": []}
    server = ThreadingHTTPServer(("0.0.0.0", 0), RoutingReplayHandler)
    server.routing_state = state
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    engine = create_async_engine(os.environ["POSTGRES_URL"], poolclass=NullPool)
    sessions = async_sessionmaker(engine)
    runs, threads = [], []
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
                "display_name": "选库协议回放",
                "provider_type": "openai",
                "base_url": f"http://api:{server.server_port}/v1",
                "api_key": "ci-replay-key",
                "capabilities": ["chat"],
                "enabled_models": [{"id": "deterministic-chat", "type": "chat", "source": "manual"}],
                "is_enabled": True,
            },
        )
        assert response.status_code == 200, response.text
        async with sessions.begin() as session:
            for kb_id, description in zip(kb_ids, ("差旅额度制度", "项目技术规范"), strict=True):
                session.add(
                    KnowledgeBase(
                        kb_id=kb_id,
                        name=kb_id,
                        description=description,
                        kb_type="dify",
                        created_by=uid,
                        share_config=share,
                        additional_params={
                            "dify_api_url": f"http://api:{server.server_port}/v1",
                            "dify_token": "test-kb-key",
                            "dify_dataset_id": kb_id,
                        },
                    )
                )
        response = await e2e_client.post(
            "/api/agent",
            headers=e2e_headers,
            json={
                "name": slug,
                "slug": slug,
                "backend_id": "ChatbotAgent",
                "share_config": share,
                "config_json": {
                    "context": {
                        "model": f"{provider_id}:deterministic-chat",
                        "tools": [],
                        "knowledges": kb_ids,
                        "mcps": [],
                        "skills": ["knowledge-base"],
                        "preload_skills": ["knowledge-base"],
                        "subagents": [],
                        "knowledge_task_scope": ["forged-config"],
                    }
                },
            },
        )
        assert response.status_code == 200, response.text
        response = await e2e_client.post(
            "/api/chat/thread",
            headers=e2e_headers,
            json={
                "agent_id": slug,
                "title": make_test_conversation_title("knowledge-routing"),
                "metadata": {
                    **make_test_conversation_metadata("knowledge-routing", e2e=True),
                    "knowledge_task_scope": ["forged-thread"],
                },
            },
        )
        assert response.status_code == 200, response.text
        threads.append(str(response.json()["id"]))
        await asyncio.sleep(5.1)  # 已有跨进程模型目录快照的有效期。
        yield dict(state=state, ids=kb_ids, slug=slug, thread_id=threads[0], runs=runs, sessions=sessions)
    finally:
        for run_id in runs:
            await cancel_run(e2e_client, e2e_headers, run_id)
        for thread_id in threads:
            response = await e2e_client.delete(f"/api/chat/thread/{thread_id}", headers=e2e_headers)
            assert response.status_code in {200, 404}, response.text
        await delete_agent(e2e_client, e2e_headers, slug)
        response = await e2e_client.delete(f"/api/system/model-providers/{provider_id}", headers=e2e_headers)
        assert response.status_code in {200, 404}, response.text
        async with Redis.from_url(os.environ["REDIS_URL"]) as redis:
            await redis.delete(*(f"{KNOWLEDGE_BASE_CACHE_KEY_PREFIX}{kb_id}" for kb_id in kb_ids))
        async with sessions.begin() as session:
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.kb_id.in_(kb_ids)))
        await engine.dispose()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)


async def run_routing_round(client, headers, case, query, route):
    """提交请求并回读同 Run 的审计、最终消息和会话任务范围。"""
    case["state"]["route"] = route
    case["state"]["tool_call_id"] = "routing-tool-" + uuid.uuid4().hex
    case["state"]["tool_results"] = []
    case["state"]["retrievals"].clear()
    response = await client.post(
        "/api/agent/runs",
        headers=headers,
        json={
            "agent_slug": case["slug"],
            "thread_id": case["thread_id"],
            "query": query,
            "meta": {"request_id": f"routing-{uuid.uuid4().hex}", "knowledge_task_scope": ["forged-meta"]},
        },
    )
    assert response.status_code == 200, response.text
    run_id = str(response.json()["run_id"])
    case["runs"].append(run_id)
    await consume_events(client, headers, run_id)
    run = await wait_for_run(client, headers, run_id)
    conn = await asyncpg.connect(postgres_dsn())
    try:
        row = await conn.fetchrow("SELECT input_payload, status, output_message_id FROM agent_runs WHERE id=$1", run_id)
        payload = json.loads(row["input_payload"])
        saved = await conn.fetchrow(
            "SELECT content, extra_metadata FROM messages WHERE id=$1", row["output_message_id"]
        )
        metadata = await conn.fetchval("SELECT extra_metadata FROM conversations WHERE thread_id=$1", case["thread_id"])
    finally:
        await conn.close()
    return run, payload, saved, json.loads(metadata)


async def test_description_routing_actual_queries_and_multiturn_scope(e2e_client, e2e_headers, routing_environment):
    """固定问题改变描述，校验零/多库、续问不重查、新任务重置及实际内容注入。"""
    case = routing_environment
    a, b = case["ids"]
    run, payload, saved, metadata = await run_routing_round(e2e_client, e2e_headers, case, "请提供实施建议", {})
    assert run["status"] == "completed", run
    assert payload["knowledge_retrieval"]["status"] == "skip"
    assert payload["knowledge_task_scope"] is None
    assert set(payload["knowledge_allowed_kb_ids"]) == {a, b}
    assert case["state"]["retrievals"] == []
    assert "没有知识库内容依据" in saved["content"]
    async with case["sessions"].begin() as session:
        await session.execute(
            update(KnowledgeBase).where(KnowledgeBase.kb_id.in_([a, b])).values(description="实施参考")
        )
    run, payload, saved, metadata = await run_routing_round(
        e2e_client, e2e_headers, case, "请提供实施建议", {"descriptions": ["实施参考"], "scope": [a, b]}
    )
    assert run["status"] == "completed", run
    assert set(case["state"]["retrievals"]) == {a, b}
    assert payload["knowledge_retrieval"]["result_count"] == 2
    assert all("EVIDENCE_" + kb_id in saved["content"] for kb_id in [a, b])
    assert set(metadata["knowledge_task_scope"]) == {a, b}
    run, payload, saved, metadata = await run_routing_round(e2e_client, e2e_headers, case, "把建议整理为表格", {})
    assert run["status"] == "completed", run
    assert case["state"]["retrievals"] == []
    assert set(payload["knowledge_task_scope"]) == {a, b}
    assert case["state"]["selections"][-1]["history"]
    run, payload, saved, metadata = await run_routing_round(
        e2e_client, e2e_headers, case, "开始一个新的设计任务", {"relation": "new"}
    )
    assert run["status"] == "completed", run
    assert metadata["knowledge_task_scope"] is None
    assert payload["knowledge_selected_kb_ids"] == []
    assert case["state"]["retrievals"] == []


async def test_description_missing_invalid_selection_empty_and_failed_retrieval(
    e2e_client, e2e_headers, routing_environment
):
    """缺描述可澄清；非法模型与内容服务失败不能变成成功空选择或扩大检索。"""
    case = routing_environment
    a, b = case["ids"]
    async with case["sessions"].begin() as session:
        await session.execute(update(KnowledgeBase).where(KnowledgeBase.kb_id == a).values(description=" "))
    run, payload, saved, metadata = await run_routing_round(
        e2e_client, e2e_headers, case, "这件事怎么办", {"clarification": "你需要哪方面资料？"}
    )
    assert run["status"] == "completed", run
    assert payload["knowledge_retrieval"]["status"] == "clarify"
    assert any(item["missing_description"] for item in payload["knowledge_retrieval"]["assessments"])
    assert "补充描述" in str(case["state"]["model_inputs"][-1])
    assert case["state"]["retrievals"] == []
    run, payload, saved, metadata = await run_routing_round(
        e2e_client, e2e_headers, case, "查询资料", {"invalid": True}
    )
    assert run["status"] == "failed", run
    assert payload["knowledge_retrieval"]["status"] == "error"
    assert payload["knowledge_retrieval"]["error_code"] == "invalid_selection"
    assert case["state"]["retrievals"] == []
    for empty, error in [(True, False), (False, True)]:
        run, payload, saved, metadata = await run_routing_round(
            e2e_client,
            e2e_headers,
            case,
            "只查技术规范",
            {"scope": [b], "descriptions": ["项目技术规范"], "empty": empty, "retrieval_error": error},
        )
        assert set(case["state"]["retrievals"]) == {b}
        assert run["status"] == ("failed" if error else "completed"), run
        assert payload["knowledge_retrieval"]["status"] == ("error" if error else "search")
        assert payload["knowledge_retrieval"]["result_count"] == 0


async def test_tools_cannot_escape_task_scope_or_use_revoked_access(e2e_client, e2e_headers, routing_environment):
    """真实模型工具调用必须在后端拒绝越界，撤权后的第二次检索不能触达连接器。"""
    case = routing_environment
    a, b = case["ids"]
    for tool in [
        {"name": "query_kb", "arguments": {"kb_id": b, "query_text": "越界查询"}},
        {"name": "open_kb_document", "arguments": {"kb_id": b, "file_id": "file-1"}},
    ]:
        run, payload, saved, metadata = await run_routing_round(
            e2e_client,
            e2e_headers,
            case,
            "仅允许使用差旅资料",
            {
                "scope": [a],
                "descriptions": ["差旅额度制度"],
                "tool": tool,
            },
        )
        assert run["status"] == "completed", run
        assert case["state"]["retrievals"] == [a]
        assert "不存在或当前会话未启用" in saved["content"], saved["content"]
        assert payload["knowledge_allowed_kb_ids"] == [a]

    async def revoke_read_access():
        """在真实模型发出工具调用前修改已隔离的 PostgreSQL 读取授权。"""
        conn = await asyncpg.connect(postgres_dsn())
        try:
            await conn.execute(
                "UPDATE knowledge_bases SET created_by='other-test-owner', share_config=$1::jsonb WHERE kb_id=$2",
                json.dumps(
                    {
                        "version": 2,
                        "read_scope": {"access_level": "user", "user_uids": ["other-test-owner"], "department_ids": []},
                        "manage_scope": None,
                    }
                ),
                a,
            )
        finally:
            await conn.close()

    case["state"]["before_tool"] = lambda: asyncio.run(revoke_read_access())
    run, payload, saved, metadata = await run_routing_round(
        e2e_client,
        e2e_headers,
        case,
        "再核查一次差旅原文",
        {
            "descriptions": ["差旅额度制度"],
            "tool": {"name": "query_kb", "arguments": {"kb_id": a, "query_text": "撤权后重试"}},
        },
    )
    assert run["status"] == "completed", run
    assert case["state"]["retrievals"] == [a], "撤权后工具仍触达内容后端"
    assert "无法获取当前会话可访问的知识库" in saved["content"], saved["content"]
