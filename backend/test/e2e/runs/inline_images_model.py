"""隔离 E2E 的图片能力协议回放，记录实际收到的消息供独立回读。"""

import json
import base64
import hashlib
import hmac
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

lock = threading.Lock()
requests = []


class Model(BaseHTTPRequestHandler):
    """提供支持图片、拒绝图片与视觉转述三种确定性行为。"""

    def log_message(self, *args):
        """不记录消息与图片正文。"""

    def do_POST(self):
        """将真实 wire 输入持久化，并返回标准 OpenAI 协议响应。"""
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        uid = self.headers.get("x-yuxi-uid", "")
        timestamp = self.headers.get("x-yuxi-uid-ts", "")
        expected = base64.b64encode(
            hmac.new(
                os.environ["YUXI_UID_SIGNATURE_SECRET"].encode(), f"uid={uid}\nts={timestamp}".encode(), hashlib.sha256
            ).digest()
        ).decode()
        if (
            uid != "inline_images_test"
            or not timestamp.isdigit()
            or abs(time.time() - int(timestamp)) > 300
            or not hmac.compare_digest(expected, self.headers.get("x-yuxi-uid-sig", ""))
        ):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"UID signature rejected","type":"authentication_error"}}')
            return
        body["test_signed_uid"] = uid
        with lock:
            requests.append(body)
            Path("/state/image-model-requests.json").write_text(json.dumps(requests))
        images = [
            block
            for message in body["messages"]
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "image_url"
        ]
        if body["model"] in {"text", "unknown"} and images:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"error": {"message": "Unexpected item type in content.", "type": "invalid_request_error"}}
                ).encode()
            )
            return
        content = "合成视觉转述：两张参考图。" if body["model"] == "vision" else "INLINE_IMAGES_DONE"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
        self.end_headers()
        if not body.get("stream"):
            self.wfile.write(
                json.dumps(
                    {
                        "id": "vision",
                        "model": body["model"],
                        "choices": [
                            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                        ],
                    }
                ).encode()
            )
            return
        for delta, finish in [({"role": "assistant", "content": content}, None), ({}, "stop")]:
            chunk = {
                "id": "images",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8777), Model).serve_forever()
