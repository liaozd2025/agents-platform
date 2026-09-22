"""仅供隔离 E2E 使用的确定性模型；在工具执行前提供 HTTP 屏障。"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ready = threading.Event()
release = threading.Event()
record = {}


class Handler(BaseHTTPRequestHandler):
    """模拟一次文件搜索和根据真实工具结果生成的终答。"""

    def log_message(self, *args):
        """避免测试请求进入访问日志。"""

    def respond(self, status, body):
        """返回控制接口的 JSON。"""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        """查询模型是否已取得工具并到达屏障。"""
        self.respond(200, {"ready": ready.is_set(), **record})

    def do_POST(self):
        """阻塞首次模型响应，收到真实工具结果后返回可核对的终答。"""
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
        if self.path == "/control/reset":
            ready.clear()
            release.clear()
            record.clear()
            return self.respond(200, {})
        if self.path == "/control/release":
            release.set()
            return self.respond(200, {})
        if self.path != "/v1/chat/completions":
            return self.respond(404, {})
        results = [message for message in body["messages"] if message.get("role") == "tool"]
        if not results:
            record["has_search_tool"] = any(t["function"]["name"] == "search_file" for t in body.get("tools", []))
            ready.set()
            if not release.wait(90):
                return self.respond(504, {"error": "test barrier timeout"})
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "kb-revocation-search",
                        "type": "function",
                        "function": {"name": "search_file", "arguments": '{"query":"revocation-sentinel"}'},
                    }
                ],
            }
            reason = "tool_calls"
        else:
            record["leaked"] = "revocation-sentinel.txt" in json.dumps(results)
            delta = {
                "role": "assistant",
                "content": "PRIVATE_FILE_OBSERVED" if record["leaked"] else "PRIVATE_FILE_DENIED",
            }
            reason = "stop"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for value, finish in [(delta, None), ({}, reason)]:
            chunk = {
                "id": "chatcmpl-revocation",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "revocation",
                "choices": [{"index": 0, "delta": value, "finish_reason": finish}],
            }
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8777), Handler).serve_forever()
