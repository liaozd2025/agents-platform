"""隔离 E2E：确定性模型和先写计数、后返回的 MCP 工具。"""

import asyncio
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp.server.fastmcp import FastMCP

state = Path("/state")
mcp = FastMCP("retry-test", host="0.0.0.0", port=8778, stateless_http=True, json_response=True)


@mcp.tool()
async def record_effect(marker: str) -> str:
    """先记录合成副作用，再等待测试允许返回；不执行任何业务操作。"""
    if marker not in {"normal", "stopped"}:
        raise ValueError("unexpected test marker")
    with (state / "effects.jsonl").open("a") as file:
        file.write(json.dumps({"marker": marker, "at": time.time()}) + "\n")
    for _ in range(1200):
        if (state / f"release-{marker}").exists():
            return "EFFECT:" + marker
        await asyncio.sleep(0.1)
    raise TimeoutError("test did not release effect response")


class ModelHandler(BaseHTTPRequestHandler):
    """每个用户输入产生一次工具调用，工具结果后给出终答。"""

    def log_message(self, *args):
        """不记录测试请求正文。"""

    def do_POST(self):
        """按真实消息末尾角色模拟模型，重放原输入会再次调用工具。"""
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body["messages"]
        marker = next(message["content"] for message in reversed(messages) if message["role"] == "user")
        if messages[-1]["role"] != "tool":
            name = next(
                tool["function"]["name"] for tool in body["tools"] if tool["function"]["name"].endswith("record_effect")
            )
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-" + uuid.uuid4().hex,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps({"marker": marker})},
                    }
                ],
            }
            reason = "tool_calls"
        else:
            delta = {"role": "assistant", "content": "DONE:" + marker}
            reason = "stop"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for value, finish in [(delta, None), ({}, reason)]:
            chunk = {
                "id": "chatcmpl-retry",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "retry-test",
                "choices": [{"index": 0, "delta": value, "finish_reason": finish}],
            }
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")


if __name__ == "__main__":
    threading.Thread(target=lambda: mcp.run(transport="streamable-http"), daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8777), ModelHandler).serve_forever()
