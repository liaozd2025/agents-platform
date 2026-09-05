"""PI HTTP E2E 的受控模型上游；文件和命令仍由真实 PI 工具执行。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CASE = re.compile(r"YUXI_PI_HTTP:([0-9a-f]{32}):(artifact|cancel|steer)")
OBSERVATIONS: dict[str, list[dict]] = {}
LOCK = threading.Lock()


def plan_response(request: dict) -> tuple[str, str, dict | str]:
    """按实际模型输入与工具结果选择固定响应，拒绝跳过真实工具的路径。"""
    if request.get("model") != "pi-http-controlled" or request.get("stream") is not True:
        raise ValueError("invalid_model_or_stream")
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages_required")
    user_texts = [str(message.get("content", "")) for message in messages if message.get("role") == "user"]
    match = next((match for text in reversed(user_texts) if (match := CASE.search(text))), None)
    if match is None:
        raise ValueError("case_marker_missing")
    nonce, scenario = match.groups()
    names = {tool.get("function", {}).get("name") for tool in request.get("tools", [])}
    task_index = next(
        index
        for index in range(len(messages) - 1, -1, -1)
        if messages[index].get("role") == "user" and CASE.search(str(messages[index].get("content", "")))
    )
    tool_messages = [message for message in messages[task_index + 1 :] if message.get("role") == "tool"]

    if "pi_sandbox" in names:
        if not tool_messages:
            return nonce, "parent_delegate", {"name": "pi_sandbox", "args": {"description": user_texts[-1]}}
        if scenario != "artifact" or "PI_HTTP_CHILD_OK" not in json.dumps(tool_messages, ensure_ascii=False):
            raise ValueError("child_result_missing")
        if "large.bin" not in json.dumps(tool_messages):
            raise ValueError("child_artifact_missing")
        return nonce, "parent_complete", "PI_HTTP_ROOT_OK"

    if not {"bash", "submit_artifact"} <= names:
        raise ValueError("pi_tools_missing")
    prompt = user_texts[-1]
    expected_history = re.search(r"YUXI_PI_EXPECT_HISTORY:([0-9a-f]{32})", prompt)
    if expected_history and f"YUXI_PI_HTTP:{expected_history[1]}:" not in json.dumps(messages[:task_index]):
        raise ValueError("previous_pi_session_missing")
    project_match = re.search(r"Project workspace is (/home/gem/user-data/projects/[0-9a-f-]+)\.", prompt)
    output_match = re.search(r"Put every generated deliverable under (/[^\s]+)\. ", prompt)
    if not project_match or not output_match:
        raise ValueError("assigned_directories_missing")
    project, output = project_match[1], output_match[1]
    if not re.fullmatch(re.escape(project) + r"/outputs/pi-runs/[0-9a-f]{24}", output):
        raise ValueError("assigned_directory_not_attempt_local")

    if not tool_messages:
        if scenario == "artifact":
            command = (
                f"pwd > cwd.txt; mkdir -p node_modules {shlex.quote(output + '/cache')}; "
                "for n in $(seq 1 220); do echo dependency > node_modules/$n; "
                f"echo cache > {shlex.quote(output + '/cache')}/$n; done; "
                f"head -c 9437184 /dev/zero > {shlex.quote(output + '/large.bin')}; "
                "printf 'PI_HTTP_CREATE_OK\\n'"
            )
        elif scenario == "steer":
            script = (
                "import time\nfrom pathlib import Path\n"
                "Path('steer-started').write_text('started')\n"
                "print('PI_HTTP_STEER_RUNNING',flush=True)\n"
                "for _ in range(300):\n"
                " if Path('steer-release').exists(): break\n"
                " time.sleep(.1)\n"
                "else: raise RuntimeError('steer release timed out')\n"
                "Path('steer-finished').write_text('finished')\n"
                "print('PI_HTTP_STEER_FINISHED',flush=True)\n"
            )
            command = f"python -u -c {shlex.quote(script)}"
        else:
            script = (
                "import os,time\nfrom pathlib import Path\n"
                "Path('cancel-writer.pid').write_text(str(os.getpid()))\n"
                "with open('cancel-growth.log','ab',buffering=0) as output:\n"
                " while True:\n  output.write(b'tick\\n')\n  print('PI_HTTP_WRITING',flush=True)\n  time.sleep(.1)\n"
            )
            command = f"python -u -c {shlex.quote(script)}"
        return nonce, "pi_execute", {"name": "bash", "args": {"command": command}}

    if scenario in {"cancel", "steer"}:
        raise ValueError("stopped_task_unexpectedly_called_model_again")
    results = json.dumps(tool_messages, ensure_ascii=False)
    if "PI_HTTP_CREATE_OK" not in results:
        raise ValueError("real_bash_result_missing")
    submitted = any(message.get("tool_call_id", "").startswith("pi_submit-") for message in tool_messages)
    if not submitted:
        return nonce, "pi_submit", {"name": "submit_artifact", "args": {"path": "large.bin"}}
    if "已登记 large.bin (9437184 bytes)" not in results:
        raise ValueError("real_submit_result_missing")
    return nonce, "pi_complete", "PI_HTTP_CHILD_OK"


class ReplayHandler(BaseHTTPRequestHandler):
    """只提供测试模型、健康及当前用例的脱敏观察记录。"""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        """返回测试上游健康与指定用例的阶段记录。"""
        if self.path == "/health":
            self.write_json(200, {"status": "ok", "service": "pi-http-replay"})
        elif re.fullmatch(r"/observations/[0-9a-f]{32}", self.path):
            with LOCK:
                records = list(OBSERVATIONS.get(self.path.rsplit("/", 1)[1], []))
            self.write_json(200, {"records": records})
        else:
            self.write_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        """验证模型请求并发送固定工具或文本响应。"""
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.write_json(404, {"error": "not_found"})
            return
        if self.headers.get("authorization") != "Bearer pi-http-replay-key":
            self.write_json(422, {"error": "invalid_authorization"})
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            if not 0 < length <= 2 * 1024 * 1024:
                raise ValueError("invalid_body_size")
            body = json.loads(self.rfile.read(length))
            nonce, phase, response = plan_response(body)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            self.write_json(422, {"error": str(exc)})
            return
        with LOCK:
            records = OBSERVATIONS.setdefault(nonce, [])
            if len(records) >= 20:
                self.write_json(422, {"error": "unexpected_model_loop"})
                return
            records.append({"phase": phase})
        common = {
            "id": f"pi-http-{nonce}",
            "object": "chat.completion.chunk",
            "model": body["model"],
            "created": int(time.time()),
        }
        if isinstance(response, str):
            delta, finish = {"content": response}, "stop"
        else:
            delta, finish = (
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"{phase}-{nonce}",
                            "type": "function",
                            "function": {"name": response["name"], "arguments": json.dumps(response["args"])},
                        }
                    ]
                },
                "tool_calls",
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}]},
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            },
        ]
        if phase == "pi_execute":
            chunks.insert(
                0, {"choices": [{"index": 0, "delta": {"content": "PI_HTTP_WORKING"}, "finish_reason": None}]}
            )
        try:
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps({**common, **chunk})}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def write_json(self, status: int, payload: dict) -> None:
        """输出有长度的JSON响应，避免测试客户端等待连接关闭。"""
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        """测试日志不保存消息正文、请求头或认证信息。"""


def main() -> None:
    """在专用测试进程启动标准库HTTP服务。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--pid-file", type=Path)
    args = parser.parse_args()
    if args.pid_file:
        args.pid_file.write_text(str(os.getpid()), encoding="ascii")
    ThreadingHTTPServer(("0.0.0.0", args.port), ReplayHandler).serve_forever()


if __name__ == "__main__":
    main()
