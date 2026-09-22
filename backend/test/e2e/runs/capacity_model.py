"""四父屏障及可控长子执行，只提供本地合成 OpenAI 协议。"""

import json
import threading
import time
import uuid
from collections import Counter
from http.server import ThreadingHTTPServer

from model_failure_fixture import Model

parents = threading.Barrier(4)
release = threading.Event()
lock = threading.Lock()
calls = Counter()


class CapacityModel(Model):
    """通过真实 task 与 write_file 调用产生可独立读取的产物。"""

    def do_GET(self):
        """测试驱动回读进入点，释放取消后的模型响应。"""
        if self.path == "/release":
            release.set()
        with lock:
            self.respond(200, dict(calls))

    def do_POST(self):
        """四父先占满槽位，子任务执行真实文件写入；取消样本停在写入前。"""
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body["messages"]
        marker = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "user"))
        tools = {item["function"]["name"] for item in body.get("tools", [])}
        parent = "task" in tools
        tool_result = messages[-1]["role"] == "tool"
        kind = "parent" if parent else "child"
        key = f"{marker['case']}:{kind}:{'after' if tool_result else 'before'}"
        with lock:
            calls[key] += 1
        name = None
        if parent and not tool_result:
            if marker["case"] == "capacity":
                try:
                    parents.wait(timeout=30)
                except threading.BrokenBarrierError:
                    return self.respond(500, {"error": {"message": "four parents did not arrive"}})
            name = "subagent_start" if marker.get("wait") == "async" else "task"
            args = {"subagent_slug": "failure-child", "description": json.dumps(marker)}
        elif (
            parent
            and tool_result
            and marker.get("wait") == "async"
            and json.loads(messages[-1]["content"]).get("status") in {"started", "existing"}
        ):
            name, args = "subagent_await", {"run_id": json.loads(messages[-1]["content"])["run_id"]}
        elif not parent and not tool_result:
            if marker["case"] == "cancel" and not release.wait(30):
                return self.respond(500, {"error": {"message": "cancel test did not release model"}})
            name, args = "write_file", {"file_path": marker["path"], "content": marker["content"]}
        if name:
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-" + uuid.uuid4().hex,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            }
        else:
            delta = {"role": "assistant", "content": f"DONE:{kind}:{marker['content']}"}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for value, finish in [(delta, None), ({}, "tool_calls" if name else "stop")]:
                chunk = {
                    "id": "capacity",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "failure-test",
                    "choices": [{"index": 0, "delta": value, "finish_reason": finish}],
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # 用户取消后的连接关闭是本测试的预期。


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8777), CapacityModel).serve_forever()
