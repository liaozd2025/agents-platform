from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

from yuxi.agents.middlewares.model_input import ImageInputCompatibilityMiddleware
from yuxi.services import image_input_service


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


def test_final_model_boundary_preserves_direct_sandbox_execution_tools() -> None:
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
            ],
        ),
        handler,
    )

    assert [tool.name for tool in seen["tools"]] == [
        "read_file",
        "ls",
        "glob",
        "grep",
        "write_file",
        "edit_file",
        "execute",
        "ocr_parse_file",
    ]


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
async def test_translates_provider_image_rejection_to_backend_ocr(error_message: str, status_code: int) -> None:
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
    assert [tool.name for tool in middleware.tools] == ["ocr_parse_file"]
    assert response.result[0].content == "当前模型不支持图片输入，正在改用 OCR 工具提取图片文字。"
    assert response.result[0].tool_calls[0]["name"] == "ocr_parse_file"
    assert response.result[0].tool_calls[0]["args"] == {"file_path": "/home/gem/user-data/uploads/image.png"}


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
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": "report.md", "content": "报告"},
                            "id": "call-write",
                        }
                    ],
                    response_metadata={"stop_reason": "max_tokens"},
                )
            ]
        )

    response = await middleware.awrap_model_call(
        _request(SimpleNamespace(), [HumanMessage("生成完整报告")]),
        handler,
    )

    assert calls == 1
    assert response.result[0].tool_calls[0]["id"] == "call-write"


@pytest.mark.asyncio
async def test_continues_truncated_response_after_removing_invalid_tool_call() -> None:
    middleware = ImageInputCompatibilityMiddleware()
    requests = []

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            invalid_call = {
                "id": "call-truncated",
                "name": "write_file",
                "args": '{"file_path":"unterminated',
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

    assert response.result[0].tool_calls[0]["name"] == "ocr_parse_file"


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


# ==== 视觉转述（主模型不接受图片时交给配置的视觉模型） ====


@pytest.fixture(autouse=True)
def vision_settings(monkeypatch: pytest.MonkeyPatch) -> dict:
    """单测不访问配置存储与外部模型：默认视为"未配置视觉模型"，并清空进程内缓存。"""
    settings = {"vision_model": None}

    class _Options:
        async def get(self):
            return dict(settings)

    monkeypatch.setattr(image_input_service, "system_options", _Options())
    image_input_service._non_image_model_specs.clear()
    image_input_service._vision_transcripts.clear()
    return settings


class _FakeVisionModel:
    """视觉模型替身：记录调用入参，可按需返回转述或抛错。"""

    def __init__(self, transcript: str = "图里是一枚红色的方形图标，几乎占满画面。", error: Exception | None = None):
        self.transcript = transcript
        self.error = error
        self.calls: list = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return AIMessage(content=self.transcript)


def _yuxi_model(spec: str) -> SimpleNamespace:
    """带 yuxi 元数据的模型替身，中间件据此识别模型身份。"""
    return SimpleNamespace(metadata={"yuxi_model_spec": spec})


def _image_request(spec: str = "alibaba-cn:qwen3.7-max", text: str = "根据这张图做图生图"):
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}
    return _request(_yuxi_model(spec), [HumanMessage(content=[{"type": "text", "text": text}, image])])


def _dashscope_rejection() -> RuntimeError:
    """复现 DashScope 对非多模态模型的拒图报错（不含 image 关键词）。"""
    error = RuntimeError(
        "Error code: 400 - {'error': {'message': '<400> InternalError.Algo.InvalidParameter: "
        "The provided messages input is invalid. The error info is [Unexpected item type in content.].'}}"
    )
    error.status_code = 400
    return error


def _text_of(message) -> str:
    return "".join(block.get("text", "") for block in message.content_blocks if block.get("type") == "text")


@pytest.mark.asyncio
async def test_uses_configured_vision_model_when_provider_rejects_image(
    vision_settings: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    vision_settings["vision_model"] = "alibaba-cn:qwen3.7-flash"
    vision_model = _FakeVisionModel()
    monkeypatch.setattr("yuxi.models.chat.load_chat_model", lambda _spec, **_kwargs: vision_model)
    middleware = ImageInputCompatibilityMiddleware()
    seen = []

    async def handler(request):
        seen.append(request.messages)
        if len(seen) == 1:
            raise _dashscope_rejection()
        return ModelResponse(result=[AIMessage(content="已按参考图生成")])

    response = await middleware.awrap_model_call(_image_request(), handler)

    assert response.result[0].content == "已按参考图生成"
    assert len(seen) == 2
    # 第二次主模型调用必须去掉图片块，换成视觉转述文本（去掉即回归原缺陷：主模型再次被拒）
    assert [block["type"] for block in seen[1][0].content_blocks] == ["text", "text"]
    second_text = _text_of(seen[1][0])
    assert '<image_transcript source="alibaba-cn:qwen3.7-flash">' in second_text
    assert "图里是一枚红色的方形图标" in second_text
    assert "根据这张图做图生图" in second_text
    # 转述请求带上了图片块与原始问题
    sidecar_content = vision_model.calls[0][0].content
    assert [block["type"] for block in sidecar_content] == ["text", "image", "text"]


@pytest.mark.asyncio
async def test_known_non_image_model_transcribes_before_calling_handler(
    vision_settings: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    vision_settings["vision_model"] = "alibaba-cn:qwen3.7-flash"
    vision_model = _FakeVisionModel()
    monkeypatch.setattr("yuxi.models.chat.load_chat_model", lambda _spec, **_kwargs: vision_model)
    middleware = ImageInputCompatibilityMiddleware()
    request = _image_request()
    handler_calls = 0
    delivered = []

    async def handler(request):
        nonlocal handler_calls
        handler_calls += 1
        delivered.append(request.messages)
        if handler_calls == 1:
            raise _dashscope_rejection()
        return ModelResponse(result=[AIMessage(content="ok")])

    await middleware.awrap_model_call(request, handler)
    await middleware.awrap_model_call(request, handler)

    # 第二次请求不再先发注定失败的调用：3 次 handler（1 失败 + 2 成功），且转述只付一次费
    assert handler_calls == 3
    assert len(vision_model.calls) == 1
    assert [block["type"] for block in delivered[1][0].content_blocks] == ["text", "text"]
    assert [block["type"] for block in delivered[2][0].content_blocks] == ["text", "text"]


@pytest.mark.asyncio
async def test_falls_back_to_ocr_when_vision_model_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_load(_spec, **_kwargs):
        raise AssertionError("未配置视觉模型时不应加载模型")

    monkeypatch.setattr("yuxi.models.chat.load_chat_model", fail_load)
    middleware = ImageInputCompatibilityMiddleware()

    async def handler(_request):
        raise _dashscope_rejection()

    response = await middleware.awrap_model_call(_image_request(), handler)

    assert response.result[0].content == "当前模型无法读取图片，且没有可供 OCR 工具解析的文件路径。"


@pytest.mark.asyncio
async def test_falls_back_to_ocr_when_vision_model_call_fails(
    vision_settings: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """视觉模型不可用时仍要退回既有 OCR 兜底，而不是让本轮直接失败。"""
    vision_settings["vision_model"] = "alibaba-cn:qwen3.7-flash"
    vision_model = _FakeVisionModel(error=RuntimeError("vision model timeout"))
    monkeypatch.setattr("yuxi.models.chat.load_chat_model", lambda _spec, **_kwargs: vision_model)
    middleware = ImageInputCompatibilityMiddleware()
    path = "/home/gem/user-data/uploads/image.png"
    request = _request(
        _yuxi_model("alibaba-cn:qwen3.7-max"),
        [_read_file_call_message(path), _read_file_image_message(path)],
    )

    async def handler(_request):
        raise _dashscope_rejection()

    response = await middleware.awrap_model_call(request, handler)

    assert response.result[0].tool_calls[0]["name"] == "ocr_parse_file"
    assert response.result[0].tool_calls[0]["args"] == {"file_path": path}


@pytest.mark.asyncio
async def test_keeps_image_when_vision_model_is_the_main_model(
    vision_settings: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """视觉模型与主模型相同时不得再调一次同一模型（否则必然同样被拒）。"""
    vision_settings["vision_model"] = "alibaba-cn:qwen3.7-max"

    def fail_load(_spec, **_kwargs):
        raise AssertionError("视觉模型与主模型相同时不应加载模型")

    monkeypatch.setattr("yuxi.models.chat.load_chat_model", fail_load)
    middleware = ImageInputCompatibilityMiddleware()

    async def handler(_request):
        raise _dashscope_rejection()

    response = await middleware.awrap_model_call(_image_request(), handler)

    assert response.result[0].content == "当前模型无法读取图片，且没有可供 OCR 工具解析的文件路径。"


# ==== read_file 抄错路径的工具执行前纠正 ====


def _attachment_context_message(real_path: str) -> HumanMessage:
    return HumanMessage(
        content=[{"type": "text", "text": f"<attachment_context>\n- preview.png: {real_path}\n</attachment_context>"}]
    )


def _read_file_response(path: str, call_id: str = "call_rf") -> ModelResponse:
    return ModelResponse(result=[AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"file_path": path}, "id": call_id}])])


REAL_PATH = "/home/gem/user-data/projects/w1/uploads/9fc2ac5549a8434cb734e71009d6c16a_preview.png"
TYPED_PATH = REAL_PATH.replace("9fc2ac55", "9fc2c554")


@pytest.mark.asyncio
async def test_corrects_mistyped_read_file_path_before_tool_execution() -> None:
    """read_file 抄错 hash 时，工具执行前自动纠正为消息里的真实路径（实测缺陷回放）。"""
    middleware = ImageInputCompatibilityMiddleware()
    request = _request(_yuxi_model("alibaba-cn:qwen3.7-max"), [_attachment_context_message(REAL_PATH)])

    async def handler(_request):
        return _read_file_response(TYPED_PATH)

    response = await middleware.awrap_model_call(request, handler)

    assert response.result[0].tool_calls[0]["args"]["file_path"] == REAL_PATH


@pytest.mark.asyncio
async def test_keeps_ambiguous_read_file_path_untouched() -> None:
    """候选与两个真实路径都只差一个字符（歧义）时不纠正，避免改错模型本意。"""
    other = REAL_PATH[:-1] + "b"
    middleware = ImageInputCompatibilityMiddleware()
    request = _request(
        _yuxi_model("alibaba-cn:qwen3.7-max"),
        [_attachment_context_message(REAL_PATH), HumanMessage(content=[{"type": "text", "text": f"- {other}"}])],
    )
    ambiguous_candidate = REAL_PATH[:-1] + "x"

    async def handler(_request):
        return _read_file_response(ambiguous_candidate)

    response = await middleware.awrap_model_call(request, handler)

    assert response.result[0].tool_calls[0]["args"]["file_path"] == ambiguous_candidate


@pytest.mark.asyncio
async def test_keeps_exact_and_non_read_file_tool_calls_untouched() -> None:
    """完全匹配的路径与其他工具的路径参数都不动。"""
    middleware = ImageInputCompatibilityMiddleware()
    request = _request(_yuxi_model("alibaba-cn:qwen3.7-max"), [_attachment_context_message(REAL_PATH)])
    seen = {}

    async def handler(_request):
        seen["result"] = ModelResponse(
            result=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "read_file", "args": {"file_path": REAL_PATH}, "id": "call_ok"},
                        {"name": "execute", "args": {"command": f"cat {TYPED_PATH}"}, "id": "call_ex"},
                    ],
                )
            ]
        )
        return seen["result"]

    response = await middleware.awrap_model_call(request, handler)

    assert response.result[0].tool_calls[0]["args"]["file_path"] == REAL_PATH
    assert response.result[0].tool_calls[1]["args"]["command"] == f"cat {TYPED_PATH}"
