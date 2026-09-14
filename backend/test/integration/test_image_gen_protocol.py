"""以真实 CLI 进程、HTTP 与文件验证 Kie 协议；外部生成服务使用确定性替身。"""

import io
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from PIL import Image

pytestmark = pytest.mark.integration
SCRIPT = Path(__file__).resolve().parents[2] / "package/yuxi/agents/skills/buildin/image-gen/scripts/image_gen.py"

# 仅替换外部网络路由；实际脚本、CLI 解析、HTTP 序列化和文件行为保持不变。
DRIVER = """
import os, runpy, sys
from urllib.parse import urlsplit
from requests.adapters import HTTPAdapter
original = HTTPAdapter.send
def send(self, request, **kwargs):
    parsed = urlsplit(request.url)
    assert parsed.hostname in {'api.kie.ai', 'kieai.redpandaai.co', 'images.example.com'}
    request.url = os.environ['REPLAY_URL'] + parsed.path + ('?' + parsed.query if parsed.query else '')
    return original(self, request, **kwargs)
HTTPAdapter.send = send
script = sys.argv.pop(1)
sys.argv[0] = script
runpy.run_path(script, run_name='__main__')
"""


@pytest.mark.parametrize(
    ("model", "edit", "remote_model"),
    [
        ("gpt-image-2", False, "gpt-image-2-text-to-image"),
        ("gpt-image-2", True, "gpt-image-2-image-to-image"),
        ("nano-banana-2", False, "nano-banana-2"),
        ("nano-banana-2", True, "nano-banana-2"),
    ],
)
def test_cli_upload_submit_query_and_download_over_http(tmp_path, model, edit, remote_model):
    """两个模型均从真实进程请求生成，经过待处理状态，最终落盘有效图片。"""
    image = io.BytesIO()
    Image.new("RGB", (3, 2), "blue").save(image, format="PNG")
    received = []
    queries = 0

    class Handler(BaseHTTPRequestHandler):
        """模拟 Kie 的网络协议，不调用被测脚本的内部实现。"""

        def log_message(self, *args):
            """避免测试服务器输出请求噪声。"""

        def do_POST(self):
            """接收真实 multipart 上传和 JSON 任务创建。"""
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, dict(self.headers), body))
            data = {"taskId": "protocol-task"}
            if self.path == "/api/file-stream-upload":
                data = {"downloadUrl": "https://images.example.com/reference.png"}
            self.reply(json.dumps({"code": 200, "success": True, "data": data}).encode())

        def do_GET(self):
            """先返回排队状态，再返回图片下载地址。"""
            nonlocal queries
            received.append((self.path, dict(self.headers), b""))
            if urlsplit(self.path).path == "/result.png":
                self.reply(image.getvalue())
                return
            queries += 1
            data = {"taskId": "protocol-task", "model": remote_model, "state": "generating"}
            if queries > 1:
                data.update(state="success", resultJson='{"resultUrls":["https://images.example.com/result.png"]}')
            self.reply(json.dumps({"code": 200, "data": data}).encode())

        def reply(self, body):
            """完整写出响应体。"""
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def execute(*args):
        """在当前测试工作目录中执行真实脚本进程。"""
        env = {**os.environ, "KIE_API_KEY": "protocol-test-key", "REPLAY_URL": f"http://127.0.0.1:{server.server_port}"}
        result = subprocess.run(
            [sys.executable, "-c", DRIVER, str(SCRIPT), *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "protocol-test-key" not in result.stdout + result.stderr
        return json.loads(result.stdout)

    try:
        args = ["generate", "--prompt", "蓝色海报", "--model", model]
        if edit:
            (tmp_path / "reference.png").write_bytes(image.getvalue())
            reference = execute("upload", "--file", "reference.png")
            args.extend(["--image-url", reference["image_url"]])
        submitted = execute(*args)
        assert submitted == {"state": "submitted", "task_id": "protocol-task"}
        pending = execute("collect", "--task-id", submitted["task_id"])
        assert pending == {"state": "generating", "task_id": "protocol-task"}
        assert not (tmp_path / "outputs").exists()
        result = execute("collect", "--task-id", submitted["task_id"])
        assert result["state"] == "success"
        assert Path(result["files"][0]).read_bytes() == image.getvalue()
        creation = [body for path, _, body in received if path == "/api/v1/jobs/createTask"]
        assert len(creation) == 1
        assert json.loads(creation[0])["model"] == remote_model
        assert all(
            headers.get("Authorization") == "Bearer protocol-test-key"
            for path, headers, _ in received
            if path != "/result.png"
        )
        assert all("Authorization" not in headers for path, headers, _ in received if path == "/result.png")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
