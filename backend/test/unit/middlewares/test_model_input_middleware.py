from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

from yuxi.agents.middlewares.model_input import ImageInputCompatibilityMiddleware


def _request(model, messages, tools=None) -> ModelRequest:
    return ModelRequest(model=model, messages=messages, tools=tools or [])


def _openai_model() -> ChatOpenAI:
    return ChatOpenAI(model="test-model", api_key="test-key", base_url="https://example.com/v1")


def _read_file_image_message(path: str = "/home/gem/user-data/uploads/image.png") -> ToolMessage:
    return ToolMessage(
        content_blocks=[{"type": "image", "base64": "abc", "mime_type": "image/png"}],
        tool_call_id="call_image",
        additional_kwargs={"read_file_path": path, "read_file_media_type": "image/png"},
    )


def _read_file_call_message(path: str = "/home/gem/user-data/uploads/image.png") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "read_file", "args": {"file_path": path}, "id": "call_image"}],
    )


def test_bridges_openai_tool_images_after_parallel_tool_results_without_mutating_state() -> None:
    middleware = ImageInputCompatibilityMiddleware()
    original_messages = [
        HumanMessage("读图并列目录"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "read_file", "args": {"file_path": "image.png"}, "id": "call_image"},
                {"name": "ls", "args": {"path": "."}, "id": "call_ls"},
            ],
        ),
        ToolMessage(
            content_blocks=[{"type": "image", "base64": "abc", "mime_type": "image/png"}],
            name="read_file",
            tool_call_id="call_image",
        ),
        ToolMessage(content="['a.png']", name="ls", tool_call_id="call_ls"),
    ]
    seen = {}

    def handler(request):
        seen["messages"] = request.messages
        return ModelResponse(result=[AIMessage(content="ok")])

    middleware.wrap_model_call(_request(_openai_model(), original_messages), handler)

    messages = seen["messages"]
    assert original_messages[2].content_blocks[0]["type"] == "image"
    assert [message.type for message in messages] == ["human", "ai", "tool", "tool", "human"]
    assert messages[2].tool_call_id == "call_image"
    assert isinstance(messages[2].content, str)
    assert messages[3].tool_call_id == "call_ls"
    assert messages[4].content_blocks[1] == {
        "type": "image",
        "base64": "abc",
        "mime_type": "image/png",
    }


def test_keeps_non_openai_tool_images_unchanged() -> None:
    middleware = ImageInputCompatibilityMiddleware()
    messages = [
        _read_file_call_message(),
        ToolMessage(
            content_blocks=[{"type": "image", "base64": "abc", "mime_type": "image/png"}],
            tool_call_id="call_image",
        ),
    ]
    seen = {}

    def handler(request):
        seen["messages"] = request.messages
        return ModelResponse(result=[AIMessage(content="ok")])

    middleware.wrap_model_call(_request(SimpleNamespace(), messages), handler)

    assert seen["messages"] is messages


def test_removes_tool_result_without_any_ai_tool_call() -> None:
    middleware = ImageInputCompatibilityMiddleware()
    seen = {}

    def handler(request):
        seen["messages"] = request.messages
        return ModelResponse(result=[AIMessage(content="ok")])

    middleware.wrap_model_call(
        _request(SimpleNamespace(), [HumanMessage("继续"), ToolMessage(content="orphan", tool_call_id="missing")]),
        handler,
    )

    assert [message.type for message in seen["messages"]] == ["human"]


def test_removes_duplicate_tool_result_after_single_call() -> None:
    middleware = ImageInputCompatibilityMiddleware()
    messages = [
        AIMessage(content="", tool_calls=[{"name": "query_kb", "args": {}, "id": "call-1"}]),
        ToolMessage(content="first", tool_call_id="call-1"),
        ToolMessage(content="duplicate", tool_call_id="call-1"),
    ]
    seen = {}

    def handler(request):
        seen["messages"] = request.messages
        return ModelResponse(result=[AIMessage(content="ok")])

    middleware.wrap_model_call(_request(SimpleNamespace(), messages), handler)

    assert [message.type for message in seen["messages"]] == ["ai", "tool"]
    assert seen["messages"][1].content == "first"


def test_removes_non_contiguous_tool_call_and_late_result() -> None:
    middleware = ImageInputCompatibilityMiddleware()
    messages = [
        AIMessage(content="working", tool_calls=[{"name": "query_kb", "args": {}, "id": "call-1"}]),
        HumanMessage("continue"),
        ToolMessage(content="late", tool_call_id="call-1"),
    ]
    seen = {}

    def handler(request):
        seen["messages"] = request.messages
        return ModelResponse(result=[AIMessage(content="ok")])

    middleware.wrap_model_call(_request(SimpleNamespace(), messages), handler)

    assert [message.type for message in seen["messages"]] == ["ai", "human"]
    assert seen["messages"][0].tool_calls == []


def test_removes_orphan_tool_result_for_truncated_tool_call_before_provider_replay() -> None:
    middleware = ImageInputCompatibilityMiddleware()
    messages = [
        HumanMessage("生成月报来源索引"),
        AIMessage(
            content=[
                {"type": "text", "text": "正在写入来源索引。"},
                {
                    "type": "invalid_tool_call",
                    "id": "chatcmpl-tool-truncated",
                    "name": "write_file",
                    "args": '{"file_path":"report.sources.md","content":"unterminated',
                    "error": "Failed to parse tool call arguments as JSON",
                },
            ],
            invalid_tool_calls=[
                {
                    "id": "chatcmpl-tool-truncated",
                    "name": "write_file",
                    "args": '{"file_path":"report.sources.md","content":"unterminated',
                    "error": "invalid JSON",
                    "type": "invalid_tool_call",
                }
            ],
        ),
        ToolMessage(
            content="Tool call could not be executed - arguments were malformed or truncated.",
            tool_call_id="chatcmpl-tool-truncated",
        ),
        HumanMessage("什么进度了？"),
    ]
    seen = {}

    def handler(request):
        seen["messages"] = request.messages
        return ModelResponse(result=[AIMessage(content="ok")])

    middleware.wrap_model_call(_request(SimpleNamespace(), messages), handler)

    replay = seen["messages"]
    assert [message.type for message in replay] == ["human", "ai", "human"]
    assert replay[1].invalid_tool_calls == []
    assert all(
        not (isinstance(block, dict) and block.get("type") == "invalid_tool_call") for block in replay[1].content
    )
    assert all(getattr(message, "tool_call_id", None) != "chatcmpl-tool-truncated" for message in replay)


def test_final_model_boundary_hides_direct_sandbox_execution_tools() -> None:
    middleware = ImageInputCompatibilityMiddleware()
    seen = {}

    def handler(request):
        seen["tools"] = request.tools
        return ModelResponse(result=[AIMessage(content="ok")])

    middleware.wrap_model_call(
        _request(
            SimpleNamespace(),
            [HumanMessage("读取并修改文件")],
            [
                SimpleNamespace(name="read_file"),
                SimpleNamespace(name="ls"),
                SimpleNamespace(name="glob"),
                SimpleNamespace(name="grep"),
                SimpleNamespace(name="write_file"),
                SimpleNamespace(name="edit_file"),
                SimpleNamespace(name="execute"),
                SimpleNamespace(name="ocr_parse_file"),
                SimpleNamespace(name="pi_sandbox"),
            ],
        ),
        handler,
    )

    assert [tool.name for tool in seen["tools"]] == ["read_file", "pi_sandbox"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_message", "status_code"),
    [
        ("This model does not support image input", 400),
        ("No endpoints found that support image input", 404),
        (
            "Error code: 400 - {'code': 20041, 'message': "
            "'The model is not a VLM (Vision Language Model). Please use text-only prompts.'}",
            400,
        ),
    ],
)
async def test_translates_provider_image_rejection_to_pi_fallback(error_message: str, status_code: int) -> None:
    middleware = ImageInputCompatibilityMiddleware()
    request = _request(
        SimpleNamespace(),
        [_read_file_call_message(), _read_file_image_message()],
    )
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        error = RuntimeError(error_message)
        error.status_code = status_code
        raise error

    response = await middleware.awrap_model_call(request, handler)

    assert calls == 1
    assert middleware.tools == []
    assert response.result[0].content == "当前模型不支持图片输入，正在交给 PI Agent 在沙箱中解析。"
    assert response.result[0].tool_calls[0]["name"] == "pi_sandbox"
    assert "/home/gem/user-data/uploads/image.png" in response.result[0].tool_calls[0]["args"]["description"]


@pytest.mark.asyncio
async def test_does_not_mask_unrelated_provider_errors_when_image_is_present() -> None:
    middleware = ImageInputCompatibilityMiddleware()
    request = _request(
        SimpleNamespace(),
        [HumanMessage(content=[{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}])],
    )

    async def handler(_request):
        error = RuntimeError("invalid tool schema")
        error.status_code = 400
        raise error

    with pytest.raises(RuntimeError, match="invalid tool schema"):
        await middleware.awrap_model_call(request, handler)


@pytest.mark.asyncio
async def test_continues_text_response_truncated_by_model_output_limit() -> None:
    middleware = ImageInputCompatibilityMiddleware()
    requests = []

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return ModelResponse(
                result=[AIMessage(content="上半部分", response_metadata={"stop_reason": "max_tokens"})]
            )
        return ModelResponse(result=[AIMessage(content="下半部分", response_metadata={"stop_reason": "end_turn"})])

    response = await middleware.awrap_model_call(
        _request(SimpleNamespace(), [HumanMessage("生成完整报告")]),
        handler,
    )

    assert len(requests) == 2
    assert isinstance(requests[1].messages[-1], HumanMessage)
    assert response.result[0].content == "上半部分下半部分"
    assert response.result[0].response_metadata["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_fails_when_text_response_repeatedly_reaches_model_output_limit() -> None:
    middleware = ImageInputCompatibilityMiddleware()
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return ModelResponse(result=[AIMessage(content="未完成", response_metadata={"finish_reason": "length"})])

    with pytest.raises(RuntimeError, match="当前 Run 未完成"):
        await middleware.awrap_model_call(
            _request(SimpleNamespace(), [HumanMessage("生成完整报告")]),
            handler,
        )

    assert calls == 4


@pytest.mark.asyncio
async def test_does_not_repeat_valid_tool_call_marked_with_output_limit() -> None:
    middleware = ImageInputCompatibilityMiddleware()
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return ModelResponse(
            result=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "pi_sandbox", "args": {"description": "生成报告"}, "id": "call-pi"}],
                    response_metadata={"stop_reason": "max_tokens"},
                )
            ]
        )

    response = await middleware.awrap_model_call(
        _request(SimpleNamespace(), [HumanMessage("生成完整报告")]),
        handler,
    )

    assert calls == 1
    assert response.result[0].tool_calls[0]["id"] == "call-pi"


@pytest.mark.asyncio
async def test_continues_truncated_response_after_removing_invalid_tool_call() -> None:
    middleware = ImageInputCompatibilityMiddleware()
    requests = []

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            invalid_call = {
                "id": "call-truncated",
                "name": "pi_sandbox",
                "args": '{"description":"unterminated',
                "error": "invalid JSON",
                "type": "invalid_tool_call",
            }
            return ModelResponse(
                result=[
                    AIMessage(
                        content=[
                            {"type": "text", "text": "上半部分"},
                            {**invalid_call, "type": "invalid_tool_call"},
                        ],
                        invalid_tool_calls=[invalid_call],
                        response_metadata={"stop_reason": "max_tokens"},
                    )
                ]
            )
        replayed = request.messages[-2]
        assert isinstance(replayed, AIMessage)
        assert replayed.invalid_tool_calls == []
        assert all(
            not (isinstance(block, dict) and block.get("type") == "invalid_tool_call") for block in replayed.content
        )
        return ModelResponse(result=[AIMessage(content="下半部分", response_metadata={"stop_reason": "end_turn"})])

    await middleware.awrap_model_call(
        _request(SimpleNamespace(), [HumanMessage("生成完整报告")]),
        handler,
    )

    assert len(requests) == 2


@pytest.mark.asyncio
async def test_translates_openrouter_missing_vision_endpoint() -> None:
    middleware = ImageInputCompatibilityMiddleware()
    request = _request(
        SimpleNamespace(),
        [_read_file_call_message(), _read_file_image_message()],
    )

    async def handler(_request):
        error = RuntimeError("No endpoints found that support image input")
        error.status_code = 404
        raise error

    response = await middleware.awrap_model_call(request, handler)

    assert response.result[0].tool_calls[0]["name"] == "pi_sandbox"


def test_omits_historical_tool_image_after_ocr_fallback() -> None:
    middleware = ImageInputCompatibilityMiddleware()
    path = "/home/gem/user-data/uploads/image.png"
    messages = [
        _read_file_call_message(path),
        _read_file_image_message(path),
        AIMessage(
            content="正在改用 OCR。",
            tool_calls=[
                {
                    "name": "ocr_parse_file",
                    "args": {"file_path": path},
                    "id": "call_ocr",
                }
            ],
        ),
        ToolMessage(content="OCR result", tool_call_id="call_ocr"),
    ]
    seen = {}

    def handler(request):
        seen["messages"] = request.messages
        return ModelResponse(result=[AIMessage(content="ok")])

    middleware.wrap_model_call(_request(_openai_model(), messages), handler)

    assert [message.type for message in seen["messages"]] == ["ai", "tool", "ai", "tool"]
    assert "OCR fallback was requested" in seen["messages"][1].content


@pytest.mark.asyncio
async def test_does_not_report_malformed_image_as_unsupported_model() -> None:
    middleware = ImageInputCompatibilityMiddleware()
    request = _request(
        SimpleNamespace(),
        [HumanMessage(content=[{"type": "image_url", "image_url": {"url": "broken"}}])],
    )

    async def handler(_request):
        error = RuntimeError("image_url provided is not a valid image")
        error.status_code = 400
        raise error

    with pytest.raises(RuntimeError, match="not a valid image"):
        await middleware.awrap_model_call(request, handler)
