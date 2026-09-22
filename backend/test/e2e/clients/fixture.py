"""客户端恢复 E2E 的合成模型、空库初始化和最终持久状态检查。"""

import asyncio
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import asyncpg
import httpx


class Model(BaseHTTPRequestHandler):
    """确定性返回原用户输入，避免外部模型与付费调用。"""

    def log_message(self, *args):
        """不记录请求正文。"""

    def do_POST(self):
        """提供真实 Agent 调用的 OpenAI 协议响应。"""
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = next(m["content"] for m in reversed(body["messages"]) if m["role"] == "user")
        content = "RECOVERED:" + str(text)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
        self.end_headers()
        if not body.get("stream"):
            self.wfile.write(
                json.dumps(
                    {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}
                ).encode()
            )
            return
        for delta, finish in [({"role": "assistant", "content": content}, None), ({}, "stop")]:
            chunk = {
                "id": "recovery",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "recovery",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")


async def database_check(setup):
    """只初始化空库；验收时断言每种恢复场景恰好一个 Request、Run 和最终消息。"""
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    try:
        if setup:
            assert await conn.fetchval("SELECT count(*) FROM users") == 0, "仅允许一次性空库"
            async with httpx.AsyncClient(base_url="http://api:5050", timeout=30) as api:
                response = await api.post(
                    "/api/auth/initialize", json={"uid": "client_test", "password": "Synthetic-only-123!"}
                )
                response.raise_for_status()
                response = await api.post(
                    "/api/auth/token", data={"username": "client_test", "password": "Synthetic-only-123!"}
                )
                response.raise_for_status()
                token = response.json()["access_token"]
                api.headers["Authorization"] = "Bearer " + token
                response = await api.post(
                    "/api/system/model-providers",
                    json={
                        "provider_id": "client-test",
                        "display_name": "恢复测试模型",
                        "provider_type": "openai",
                        "base_url": "http://replay:8777/v1",
                        "api_key": "synthetic-only",
                        "capabilities": ["chat"],
                        "is_enabled": True,
                        "enabled_models": [
                            {"id": "recovery", "display_name": "recovery", "type": "chat", "source": "manual"}
                        ],
                    },
                )
                response.raise_for_status()
                response = await api.post(
                    "/api/system/config", json={"key": "fast_model", "value": "client-test:recovery"}
                )
                response.raise_for_status()
                response = await api.post(
                    "/api/agent",
                    json={
                        "name": "客户端恢复测试",
                        "slug": "client-test",
                        "backend_id": "ChatbotAgent",
                        "config_json": {
                            "context": {
                                "model": "client-test:recovery",
                                "tools": [],
                                "mcps": [],
                                "skills": [],
                                "knowledges": [],
                                "subagents": [],
                            }
                        },
                    },
                )
                response.raise_for_status()
                response = await api.post(
                    "/api/user/apikey/", json={"request_id": "client-test-key", "name": "一次性恢复测试"}
                )
                response.raise_for_status()
                path = Path("/state/client.json")
                path.write_text(json.dumps({"token": token, "key": response.json()["secret"]}))
                path.chmod(0o600)
            return
        expected = {"web-auto": 1, "web-manual": 1, "cli-first": 1, "cli-stream": 1, "cli-repeat": 2}
        for marker, count in expected.items():
            for _ in range(200):
                rows = await conn.fetch(
                    "SELECT r.id,r.status,r.runtime_cleanup_pending,m.content FROM agent_runs r "
                    "JOIN messages m ON m.id=r.output_message_id WHERE r.id IN "
                    "(SELECT run_id FROM agent_run_requests q JOIN messages i ON i.id=q.input_message_id "
                    "WHERE i.content=$1)",
                    marker,
                )
                if len(rows) == count and all(
                    r["status"] == "completed" and not r["runtime_cleanup_pending"] for r in rows
                ):
                    break
                await asyncio.sleep(0.1)
            assert len(rows) == count and all(
                r["status"] == "completed"
                and not r["runtime_cleanup_pending"]
                and r["content"] == "RECOVERED:" + marker
                for r in rows
            ), marker
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM agent_run_requests q JOIN messages m ON m.id=q.input_message_id "
                    "WHERE m.content=$1",
                    marker,
                )
                == count
            ), marker
            assert (
                await conn.fetchval("SELECT count(*) FROM messages WHERE role='user' AND content=$1", marker) == count
            ), marker
            for row in rows:
                assert await conn.fetchval("SELECT count(*) FROM agent_run_attempts WHERE run_id=$1", row["id"]) == 1, (
                    marker
                )
        assert await conn.fetchval("SELECT count(*) FROM agent_run_requests") == 6
        assert await conn.fetchval("SELECT count(*) FROM agent_runs") == 6
        assert await conn.fetchval("SELECT count(*) FROM messages WHERE role='assistant'") == 6
        print("PG: 6 Request、6 Run、6 最终消息；恢复未创建额外任务，有意重复发送创建独立任务")
    finally:
        await conn.close()


if __name__ == "__main__":
    if sys.argv[1] == "model":
        ThreadingHTTPServer(("0.0.0.0", 8777), Model).serve_forever()
    else:
        asyncio.run(database_check(sys.argv[1] == "setup"))
