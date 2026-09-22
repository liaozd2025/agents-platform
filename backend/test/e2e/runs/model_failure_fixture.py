"""仅供隔离 E2E：记录模型请求次数，提供正常、暂时失败和持续失败响应。"""

import json
import threading
import time
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

calls = Counter()
lock = threading.Lock()


class Model(BaseHTTPRequestHandler):
    """模拟真实 OpenAI 协议和父子任务工具调用，不访问外部模型。"""

    def log_message(self, *args):
        """不记录请求正文。"""

    def respond(self, status, body):
        """返回协议 JSON。"""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        """独立回读服务端请求次数。"""
        with lock:
            snapshot = dict(calls)
        self.respond(200, snapshot)

    def do_POST(self):
        """持续422耗尽重试；暂时422在第二次调用恢复；父模型调用真实 task。"""
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body["messages"]
        marker = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        if not isinstance(marker, str) or marker not in {
            f"{kind}:{mode}" for kind in ["main", "child", "delegate"] for mode in ["normal", "recover", "exhausted"]
        }:
            return self.respond(400, {"error": {"message": "unexpected synthetic marker"}})
        with lock:
            calls[marker] += 1
            attempt = calls[marker]
        kind, mode = marker.split(":")
        if kind != "delegate" and (mode == "exhausted" or (mode == "recover" and attempt == 1)):
            return self.respond(
                422, {"error": {"message": "synthetic model rejected request", "type": "invalid_request_error"}}
            )
        if kind == "delegate" and messages[-1]["role"] != "tool":
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-" + uuid.uuid4().hex,
                        "type": "function",
                        "function": {
                            "name": "task",
                            "arguments": json.dumps({"subagent_slug": "failure-child", "description": "child:" + mode}),
                        },
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
                "id": "model-failure",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "failure-test",
                "choices": [{"index": 0, "delta": value, "finish_reason": finish}],
            }
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8777), Model).serve_forever()
