"""本地索引 E2E 的确定性向量和末块故障屏障。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

mode = "healthy"
ready = threading.Event()
release = threading.Event()


class Handler(BaseHTTPRequestHandler):
    """按合成末块内容触发故障或取消屏障，不访问外部 provider。"""

    def log_message(self, *args):
        """省略测试访问日志。"""

    def respond(self, status, body):
        """返回 JSON。"""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        """读取末块是否到达。"""
        self.respond(200, {"ready": ready.is_set()})

    def do_POST(self):
        """正常生成两维向量，指定末块返回真实 HTTP 400 或等待。"""
        global mode
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
        if self.path == "/control/reset":
            mode = body["mode"]
            ready.clear()
            release.clear()
            return self.respond(200, {})
        if self.path == "/control/release":
            release.set()
            return self.respond(200, {})
        if self.path != "/v1/embeddings":
            return self.respond(404, {})
        texts = body["input"]
        if any("sentinel-200" in text for text in texts):
            ready.set()
            if mode == "fail":
                return self.respond(400, {"error": {"message": "synthetic final chunk failure"}})
            if mode == "block" and not release.wait(90):
                return self.respond(504, {"error": "test barrier timeout"})
        self.respond(200, {"data": [{"index": i, "embedding": [0.1, 0.2]} for i in range(len(texts))]})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8777), Handler).serve_forever()
