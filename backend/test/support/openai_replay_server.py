"""为 assembled-path E2E 提供最小 OpenAI 兼容确定性响应。"""

from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import parse_qs, urlparse

EXPECTED_OUTPUT = "DETERMINISTIC_AGENT_E2E_OK"
EXPECTED_AUTHORIZATION = "Bearer ci-replay-key"
EXPECTED_MODEL = "deterministic-chat"
EXPECTED_PRELOADED_SKILL_MARKER = "# 图片生成技能"
EXPECTED_PRELOADED_TOOL = "present_artifacts"
EXPECTED_TOOL_CALL_ID = "call-preloaded-tool"
EXPECTED_TOOL_RESULT_MARKER = "已将交付物展示给用户"
BLOCK_BEFORE_RESPONSE_MARKER = "DETERMINISTIC_BLOCK_BEFORE_RESPONSE"
TOOL_ERROR_MARKER = "DETERMINISTIC_TOOL_ERROR"
LARGE_TOOL_RESULT_MARKER = "DETERMINISTIC_LARGE_TOOL_RESULT"
LARGE_TOOL_CALL_ID = "call-large-tool-result"
BLOCKING_REQUEST_TOKENS: set[str] = set()
BLOCKING_REQUEST_TOKENS_LOCK = Lock()


def _validate_request(authorization: str | None, request: dict) -> str | None:
    """拒绝没有走预期模型适配契约的 replay 请求。"""

    if authorization != EXPECTED_AUTHORIZATION:
        return "invalid_authorization"
    if request.get("model") != EXPECTED_MODEL:
        return "invalid_model"
    if request.get("stream") is not True:
        return "stream_required"
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages_required"
    serialized_messages = json.dumps(messages, ensure_ascii=False)
    if "CITATION_REPLAY:" in serialized_messages:
        try:
            _citation_response(request)
        except (ValueError, KeyError, TypeError, StopIteration) as exc:
            return f"citation_contract: {exc}"
        return None
    if EXPECTED_OUTPUT not in serialized_messages:
        return "expected_input_missing"
    if EXPECTED_PRELOADED_SKILL_MARKER not in serialized_messages:
        return "preloaded_skill_missing"
    tools = request.get("tools")
    tool_names = {
        item.get("function", {}).get("name")
        for item in tools or []
        if isinstance(item, dict) and isinstance(item.get("function"), dict)
    }
    if "DETERMINISTIC_OFFICE_PATH:" in serialized_messages:
        if "ocr_parse_file" not in tool_names:
            return "office_parser_missing"
        for message in messages:
            if message.get("role") == "tool" and (
                message.get("tool_call_id") != "call-office-parser"
                or "Office backend content" not in str(message.get("content"))
                or "/outputs/ocr/" not in str(message.get("content"))
            ):
                return "office_parser_result_missing"
        return None
    subagent_child = "DETERMINISTIC_SUBAGENT_CHILD" in serialized_messages
    subagent_parent = "DETERMINISTIC_SUBAGENT_PARENT:" in serialized_messages
    if subagent_child:
        trusted = "SUBAGENT_MODE:always_trust" in serialized_messages
        if ("write_file" in tool_names) != trusted or "task" in tool_names:
            return "subagent_tool_policy_mismatch"
    elif EXPECTED_PRELOADED_TOOL not in tool_names:
        return "preloaded_tool_missing"
    if LARGE_TOOL_RESULT_MARKER in serialized_messages and "execute" not in tool_names:
        return "execute_tool_missing"
    tool_messages = [message for message in messages if isinstance(message, dict) and message.get("role") == "tool"]
    if subagent_child or subagent_parent:
        expected_call = "call-subagent-write" if subagent_child else "call-subagent-task"
        if subagent_parent and "task" not in tool_names:
            return "subagent_task_missing"
        if tool_messages and not any(message.get("tool_call_id") == expected_call for message in tool_messages):
            return "subagent_tool_result_missing"
        return None
    if tool_messages and not any(
        (
            message.get("tool_call_id") == EXPECTED_TOOL_CALL_ID
            and (
                EXPECTED_TOOL_RESULT_MARKER in str(message.get("content", ""))
                or TOOL_ERROR_MARKER in serialized_messages
            )
        )
        or (
            LARGE_TOOL_RESULT_MARKER in serialized_messages
            and message.get("tool_call_id") == LARGE_TOOL_CALL_ID
            and "Tool result too large" in str(message.get("content", ""))
        )
        for message in tool_messages
    ):
        return "tool_execution_result_missing"
    return None


def _stream_payloads(model: str, messages: list[dict]) -> list[dict]:
    serialized_messages = json.dumps(messages, ensure_ascii=False)
    common = {
        "id": "chatcmpl-yuxi-deterministic",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }
    if "CITATION_REPLAY:" in serialized_messages:
        delta, finish = _citation_response({"messages": messages})
        return [
            {**common, "choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}]},
            {
                **common,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            },
        ]
    if any(message.get("role") == "tool" for message in messages if isinstance(message, dict)):
        return [
            {
                **common,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": EXPECTED_OUTPUT},
                        "finish_reason": None,
                    }
                ],
            },
            {
                **common,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            },
        ]

    large_result = LARGE_TOOL_RESULT_MARKER in serialized_messages
    tool_call_id = LARGE_TOOL_CALL_ID if large_result else EXPECTED_TOOL_CALL_ID
    tool_name = "execute" if large_result else EXPECTED_PRELOADED_TOOL
    if "DETERMINISTIC_OFFICE_PATH:" in serialized_messages:
        tool_call_id, tool_name = "call-office-parser", "ocr_parse_file"
        path = re.search(r'DETERMINISTIC_OFFICE_PATH:(/[^\s"\\]+)', serialized_messages).group(1)
        tool_arguments = json.dumps({"file_path": path})
    elif "DETERMINISTIC_SUBAGENT_CHILD" in serialized_messages:
        tool_call_id, tool_name = "call-subagent-write", "write_file"
        path = re.search(r'SUBAGENT_PATH:(/[^\s"\\]+)', serialized_messages).group(1)
        tool_arguments = json.dumps({"file_path": path, "content": "subagent write verified"})
    elif "DETERMINISTIC_SUBAGENT_PARENT:" in serialized_messages:
        tool_call_id, tool_name = "call-subagent-task", "task"
        slug = re.search(r"DETERMINISTIC_SUBAGENT_PARENT:([\w-]+)", serialized_messages).group(1)
        description = next(message["content"] for message in reversed(messages) if message.get("role") == "user")
        tool_arguments = json.dumps({"subagent_slug": slug, "description": description})
    elif large_result:
        tool_arguments = json.dumps({"command": "yes X | head -c 13000"})
    elif TOOL_ERROR_MARKER in serialized_messages:
        tool_arguments = "{}"
    else:
        tool_arguments = '{"filepaths": []}'

    return [
        {
            **common,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": tool_arguments,
                                },
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            **common,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
        },
    ]


def _citation_response(request: dict) -> tuple[dict, str]:
    """引用回放只在真实工具返回匹配原文和子回复之后继续模型步骤。"""
    messages = request["messages"]
    payload = next(
        json.JSONDecoder().raw_decode(message["content"].split("CITATION_REPLAY:", 1)[1])[0]
        for message in reversed(messages)
        if message.get("role") == "user" and "CITATION_REPLAY:" in str(message.get("content"))
    )
    token = payload["token"]
    results = {
        message["tool_call_id"]: message["content"]
        for message in messages
        if message.get("role") == "tool" and str(message.get("tool_call_id", "")).startswith(f"call-citation-{token}-")
    }

    def call(suffix: str, name: str, arguments: dict) -> tuple[dict, str]:
        """返回单个模型工具调用，并检查 shipping 装配实际提供该工具。"""
        if "tools" in request and name not in {tool.get("function", {}).get("name") for tool in request["tools"]}:
            raise ValueError(f"missing tool {name}")
        return {
            "tool_calls": [
                {
                    "index": 0,
                    "id": f"call-citation-{token}-{suffix}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ]
        }, "tool_calls"

    if payload.get("child"):
        file_id = payload["file_ids"][0]
        tool_id = f"call-citation-{token}-open"
        if tool_id not in results:
            if f"call-citation-{token}-select" not in results:
                return call("select", "select_kbs", {"query": "CITATION_SELECT:" + payload["kb_id"]})
            return call("open", "open_kb_document", {"kb_id": payload["kb_id"], "file_id": file_id})
        document = json.loads(results[tool_id])
        if document.get("kb_id") != payload["kb_id"] or document.get("file_id") != file_id:
            raise ValueError("knowledge identity missing from real tool result")
        raw_value = re.search(r"CITATION_RAW_VALUE:([a-f0-9]+)", document.get("content", ""))
        if not raw_value:
            raise ValueError("knowledge original missing from real tool result")
        source = f"kb://{payload['kb_id']}/{file_id}"
        return {"content": f'核查摘要 {raw_value.group(1)} <cite source="{source}" type="file">1</cite>'}, "stop"

    summaries = []
    for index, file_id in enumerate(payload["file_ids"]):
        suffix = f"task-{index}"
        if payload["mode"] == "async":
            start_id = f"call-citation-{token}-start-{index}"
            if start_id not in results:
                suffix = f"start-{index}"
            else:
                result_id = f"call-citation-{token}-{suffix}"
                if result_id not in results:
                    started = json.loads(results[start_id])
                    return call(suffix, "subagent_await", {"run_id": started["run_id"]})
        result_id = f"call-citation-{token}-{suffix}"
        if result_id not in results:
            child_payload = {**payload, "child": True, "file_ids": [file_id]}
            return call(
                suffix,
                "subagent_start" if payload["mode"] == "async" else "task",
                {"subagent_slug": payload["child_slug"], "description": "CITATION_REPLAY:" + json.dumps(child_payload)},
            )
        content = results[f"call-citation-{token}-task-{index}"]
        if payload["mode"] == "async":
            content = json.loads(content)["result"]["output"]
        source = f"kb://{payload['kb_id']}/{file_id}"
        summary = re.search(
            r'核查摘要 ([a-f0-9]+) <cite source="' + re.escape(source) + r'" type="file">1</cite>', content
        )
        if not summary:
            raise ValueError("child evidence missing from real task result")
        if file_id in payload.get("adopted_file_ids", payload["file_ids"]):
            summaries.append(f'主助手汇总 {summary.group(1)} <cite source="{source}" type="file">1</cite>')

    if payload["mode"] == "resume":
        question_id = f"call-citation-{token}-question"
        if question_id not in results:
            return call(
                "question",
                "ask_user_question",
                {
                    "questions": [
                        {
                            "question_id": "citation-confirm",
                            "question": "选择输出形式",
                            "options": [{"label": "简短"}, {"label": "完整"}],
                        }
                    ]
                },
            )
        if "简短" not in results[question_id]:
            raise ValueError("resume answer missing from real tool result")
    summaries.append(f'仅链接来源 <cite source="{payload["url"]}" type="url">1</cite>')
    return {"content": "\n\n".join(summaries)}, "stop"


def selection_completion(request: dict, selection: dict) -> dict:
    """按真实结构化适配器请求返回 JSON 或工具参数，不执行应用内 monkeypatch。"""
    message = {"role": "assistant", "content": json.dumps(selection, ensure_ascii=False)}
    finish = "stop"
    tools = request.get("tools") or []
    if tools:
        message["content"] = None
        message["tool_calls"] = [
            {
                "id": "call-selection",
                "type": "function",
                "function": {"name": tools[0]["function"]["name"], "arguments": json.dumps(selection)},
            }
        ]
        finish = "tool_calls"
    return {
        "id": "selection-replay",
        "object": "chat.completion",
        "created": 1,
        "model": request["model"],
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class ReplayHandler(BaseHTTPRequestHandler):
    """只实现测试所需的 health 与 chat completions 协议。"""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._write_json(200, {"status": "ok"})
            return
        if parsed.path == "/blocking-started":
            token = (parse_qs(parsed.query).get("token") or [""])[0]
            with BLOCKING_REQUEST_TOKENS_LOCK:
                started = token in BLOCKING_REQUEST_TOKENS
            self._write_json(200, {"started": started})
            return
        self._write_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._write_json(404, {"error": "not_found"})
            return

        try:
            length = int(self.headers.get("content-length", "0"))
            request = json.loads(self.rfile.read(length) or b"{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            self._write_json(400, {"error": "invalid_json"})
            return

        selection_request = any(
            item.get("function", {}).get("name") == "KnowledgeSelection" for item in request.get("tools") or []
        ) or (request.get("response_format", {}).get("json_schema", {}).get("name") == "KnowledgeSelection")
        if (
            selection_request
            and request.get("stream") is not True
            and self.headers.get("authorization") == EXPECTED_AUTHORIZATION
            and request.get("model") == EXPECTED_MODEL
        ):
            try:
                routing = json.loads(request["messages"][-1]["content"])
                catalog = routing["catalog"]
            except (ValueError, KeyError, TypeError):
                self._write_json(422, {"error": "invalid_selection_request"})
                return
            query = routing["query"]
            selected_id = query.removeprefix("CITATION_SELECT:") if query.startswith("CITATION_SELECT:") else None
            selection = {
                "task_relation": "continue",
                "explicit_scope": None,
                "requested_kb_ids": [],
                "clarification": None,
                "assessments": [
                    {
                        "kb_id": item["kb_id"],
                        "status": "select" if item["kb_id"] == selected_id else "skip",
                        "reason": "原文读取需要此库" if item["kb_id"] == selected_id else "本轮无需内容检索",
                    }
                    for item in catalog
                ],
            }
            self._write_json(200, selection_completion(request, selection))
            return

        request_error = _validate_request(self.headers.get("authorization"), request)
        if request_error:
            self._write_json(422, {"error": request_error})
            return

        serialized_messages = json.dumps(request["messages"], ensure_ascii=False)
        blocking_match = re.search(rf"{BLOCK_BEFORE_RESPONSE_MARKER}:([0-9a-f-]+)", serialized_messages)
        model = str(request["model"])
        payloads = _stream_payloads(model, request["messages"])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        if blocking_match:
            self.wfile.write(f"data: {json.dumps(payloads.pop(0))}\n\n".encode())
            self.wfile.flush()
            with BLOCKING_REQUEST_TOKENS_LOCK:
                BLOCKING_REQUEST_TOKENS.add(blocking_match.group(1))
            time.sleep(60)
        for payload in payloads:
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        return

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), ReplayHandler).serve_forever()


if __name__ == "__main__":
    main()
