from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, merge_message_runs
from langchain_openai import ChatOpenAI

from yuxi.agents.toolkits.buildin.tools import ocr_parse_file
from yuxi.services.image_input_service import (
    closest_real_path,
    collect_absolute_paths,
    is_known_non_image_model,
    mark_non_image_model,
    message_has_image,
    message_text,
    model_spec_of,
    transcribe_message_images,
)
from yuxi.utils.logging_config import logger

_TOOL_IMAGE_USER_TEXT = "Images returned by read_file are attached below. Inspect them when answering."
_INVALID_TOOL_CALL_TEXT = (
    "A previous tool call could not be parsed and was not executed. "
    "Retry it with valid JSON arguments if it is still needed."
)
_IMAGE_ERROR_TERMS = ("image", "vision", "multimodal", "multi-modal")
_REJECTION_TERMS = (
    "does not support",
    "no endpoints found that support",
    "not allowed",
    "not a vlm",
    "not supported",
    "text-only prompts",
    "unsupported",
)
# 部分供应商不点明"图片"，只说 content 项类型非法（如 DashScope 的
# "Unexpected item type in content."）。调用方已确认本轮消息确实带图片，可直接判定为图片被拒。
_CONTENT_TYPE_REJECTION_TERMS = ("unexpected item type in content",)
_TRUNCATED_STOP_REASONS = {"length", "max_output_tokens", "max_tokens"}
_MAX_TRUNCATION_CONTINUATIONS = 3
_CONTINUE_TRUNCATED_RESPONSE = (
    "Continue exactly where the previous response stopped. Do not repeat completed content. "
    "Finish the active task, including any required tool calls and deliverables."
)


class ImageInputCompatibilityMiddleware(AgentMiddleware[Any, Any, Any]):
    """Bridge OpenAI tool images and translate explicit image capability errors."""

    tools = [ocr_parse_file]

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        request = _normalize_invalid_tool_calls(request)
        image_paths = _read_file_image_paths(request.messages)
        request = _bridge_openai_tool_images(request)

        def correcting_handler(req: ModelRequest) -> ModelResponse:
            return _fix_mistyped_read_file_paths(req, handler(req))

        try:
            return _continue_sync_model_response(request, correcting_handler)
        except Exception as exc:  # noqa: BLE001
            if _has_image(request.messages) and _is_image_input_rejection(exc):
                return _ocr_fallback_response(image_paths)
            raise

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        request = _normalize_invalid_tool_calls(request)
        image_paths = _read_file_image_paths(request.messages)
        request = _bridge_openai_tool_images(request)

        async def correcting_handler(req: ModelRequest) -> ModelResponse:
            return _fix_mistyped_read_file_paths(req, await handler(req))

        try:
            # 已知该模型不接受图片时直接转述，不再发一次注定失败的请求
            if _has_image(request.messages) and is_known_non_image_model(model_spec_of(request.model)):
                transcribed = await _transcribe_images(request)
                if transcribed is not None:
                    return await _continue_async_model_response(transcribed, correcting_handler)
            return await _continue_async_model_response(request, correcting_handler)
        except Exception as exc:  # noqa: BLE001
            if not (_has_image(request.messages) and _is_image_input_rejection(exc)):
                raise
            mark_non_image_model(model_spec_of(request.model))
            logger.info(f"模型 {model_spec_of(request.model)} 拒绝了图片输入，改为视觉模型转述后重试")
            # 先把图片交给视觉模型转述，让不支持图片的主模型也能继续；不可用时退回 OCR 兜底
            transcribed = await _transcribe_images(request)
            if transcribed is not None:
                try:
                    return await _continue_async_model_response(transcribed, correcting_handler)
                except Exception as retry_exc:  # noqa: BLE001
                    logger.warning(f"视觉转述后主模型仍然失败，退回 OCR 兜底: {retry_exc}")
            return _ocr_fallback_response(image_paths)


def _continue_sync_model_response(
    request: ModelRequest,
    handler: Callable[[ModelRequest], ModelResponse],
) -> ModelResponse:
    """在同一次模型节点内续写被输出上限截断的纯文本响应。"""

    responses = []
    for continuation in range(_MAX_TRUNCATION_CONTINUATIONS + 1):
        response = handler(request)
        truncated = _response_was_truncated(response)
        if truncated:
            response = _normalize_invalid_response_tool_calls(response)
        responses.append(response)
        if not truncated:
            return _merge_continued_responses(responses)
        if continuation == _MAX_TRUNCATION_CONTINUATIONS:
            raise RuntimeError("模型响应连续达到输出上限，当前 Run 未完成")
        request = request.override(
            messages=[*request.messages, *response.result, HumanMessage(_CONTINUE_TRUNCATED_RESPONSE)]
        )
    raise AssertionError("unreachable")


async def _continue_async_model_response(
    request: ModelRequest,
    handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
) -> ModelResponse:
    """异步续写被输出上限截断的纯文本响应。"""

    responses = []
    for continuation in range(_MAX_TRUNCATION_CONTINUATIONS + 1):
        response = await handler(request)
        truncated = _response_was_truncated(response)
        if truncated:
            response = _normalize_invalid_response_tool_calls(response)
        responses.append(response)
        if not truncated:
            return _merge_continued_responses(responses)
        if continuation == _MAX_TRUNCATION_CONTINUATIONS:
            raise RuntimeError("模型响应连续达到输出上限，当前 Run 未完成")
        request = request.override(
            messages=[*request.messages, *response.result, HumanMessage(_CONTINUE_TRUNCATED_RESPONSE)]
        )
    raise AssertionError("unreachable")


def _response_was_truncated(response: ModelResponse) -> bool:
    if not response.result:
        return False
    message = response.result[-1]
    if not isinstance(message, AIMessage) or message.tool_calls:
        return False
    metadata = message.response_metadata or {}
    stop_reason = str(metadata.get("stop_reason") or metadata.get("finish_reason") or "").lower()
    return stop_reason in _TRUNCATED_STOP_REASONS


def _merge_continued_responses(responses: list[ModelResponse]) -> ModelResponse:
    if len(responses) == 1:
        return responses[0]
    messages = merge_message_runs(
        [message for response in responses for message in response.result],
        chunk_separator="",
    )
    final_message = responses[-1].result[-1]
    if len(messages) == 1 and isinstance(messages[0], AIMessage) and isinstance(final_message, AIMessage):
        messages[0] = messages[0].model_copy(update={"response_metadata": final_message.response_metadata})
    return ModelResponse(result=messages, structured_response=responses[-1].structured_response)


def _normalize_invalid_ai_message(message: AIMessage) -> tuple[AIMessage, bool]:
    content = message.content
    valid_blocks = content
    if isinstance(content, list):
        valid_blocks = [
            block for block in content if not (isinstance(block, dict) and block.get("type") == "invalid_tool_call")
        ]
    removed_block = isinstance(content, list) and len(valid_blocks) != len(content)
    if not removed_block and not message.invalid_tool_calls:
        return message, False

    if isinstance(valid_blocks, list):
        normalized_content = [*valid_blocks, {"type": "text", "text": _INVALID_TOOL_CALL_TEXT}]
    else:
        normalized_content = f"{valid_blocks}\n\n{_INVALID_TOOL_CALL_TEXT}" if valid_blocks else _INVALID_TOOL_CALL_TEXT
    return message.model_copy(update={"content": normalized_content, "invalid_tool_calls": []}), True


def _normalize_invalid_response_tool_calls(response: ModelResponse) -> ModelResponse:
    result = []
    changed = False
    for message in response.result:
        if isinstance(message, AIMessage):
            message, message_changed = _normalize_invalid_ai_message(message)
            changed = changed or message_changed
        result.append(message)
    if not changed:
        return response
    return ModelResponse(result=result, structured_response=response.structured_response)


def _normalize_invalid_tool_calls(request: ModelRequest) -> ModelRequest:
    """移除 provider 无法配对的畸形调用及孤儿结果。"""

    messages = []
    changed = False
    index = 0
    while index < len(request.messages):
        message = request.messages[index]
        if not isinstance(message, AIMessage):
            if isinstance(message, ToolMessage):
                changed = True
                index += 1
                continue
            messages.append(message)
            index += 1
            continue

        normalized_message, message_changed = _normalize_invalid_ai_message(message)
        changed = changed or message_changed
        call_ids = {
            call["id"] for call in normalized_message.tool_calls if isinstance(call.get("id"), str) and call["id"]
        }
        if not call_ids:
            messages.append(normalized_message)
            index += 1
            continue

        tool_messages = []
        seen_ids = set()
        next_index = index + 1
        while next_index < len(request.messages) and isinstance(request.messages[next_index], ToolMessage):
            tool_message = request.messages[next_index]
            if tool_message.tool_call_id in call_ids and tool_message.tool_call_id not in seen_ids:
                tool_messages.append(tool_message)
                seen_ids.add(tool_message.tool_call_id)
            else:
                changed = True
            next_index += 1
        if seen_ids != call_ids:
            messages.append(normalized_message.model_copy(update={"tool_calls": []}))
            changed = True
        else:
            messages.extend([normalized_message, *tool_messages])
        index = next_index

    return request.override(messages=messages) if changed else request


def _bridge_openai_tool_images(request: ModelRequest) -> ModelRequest:
    if not isinstance(request.model, ChatOpenAI):
        return request

    bridged_messages = []
    pending_images: list[dict[str, Any]] = []
    latest_ocr_call_by_path: dict[str, int] = {}
    for index, message in enumerate(request.messages):
        if not isinstance(message, AIMessage):
            continue
        for tool_call in message.tool_calls:
            if tool_call.get("name") != "ocr_parse_file":
                continue
            file_path = tool_call.get("args", {}).get("file_path")
            if isinstance(file_path, str) and file_path:
                latest_ocr_call_by_path[file_path] = index

    def flush_pending_images() -> None:
        if not pending_images:
            return
        bridged_messages.append(
            HumanMessage(content_blocks=[{"type": "text", "text": _TOOL_IMAGE_USER_TEXT}, *pending_images])
        )
        pending_images.clear()

    for index, message in enumerate(request.messages):
        if not isinstance(message, ToolMessage):
            flush_pending_images()
            bridged_messages.append(message)
            continue

        image_blocks = [block for block in message.content_blocks if block.get("type") == "image"]
        if not image_blocks:
            bridged_messages.append(message)
            continue

        image_path = message.additional_kwargs.get("read_file_path")
        ocr_fallback_requested = isinstance(image_path, str) and latest_ocr_call_by_path.get(image_path, -1) > index
        if not ocr_fallback_requested:
            pending_images.extend(image_blocks)
        text = "\n".join(
            block["text"]
            for block in message.content_blocks
            if block.get("type") == "text" and isinstance(block.get("text"), str)
        )
        bridged_messages.append(
            message.model_copy(
                update={
                    "content": text
                    or (
                        f"read_file returned {len(image_blocks)} image(s). "
                        + (
                            "OCR fallback was requested for this image."
                            if ocr_fallback_requested
                            else "The image content is attached in the following user message for visual inspection."
                        )
                    )
                }
            )
        )

    flush_pending_images()
    if bridged_messages == request.messages:
        return request
    return request.override(messages=bridged_messages)


def _read_file_image_paths(messages: list[Any]) -> list[str]:
    paths: list[str] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        if not any(block.get("type") == "image" for block in message.content_blocks):
            continue
        path = message.additional_kwargs.get("read_file_path")
        if isinstance(path, str) and path and path not in paths:
            paths.append(path)
    return paths


async def _transcribe_images(request: ModelRequest) -> ModelRequest | None:
    """用视觉模型把请求里的图片逐条替换为文本转述，返回新请求；不可用时返回 None 由调用方降级。"""
    main_spec = model_spec_of(request.model)
    replacements: dict[int, Any] = {}
    for index, message in enumerate(request.messages):
        if not message_has_image(message):
            continue
        transcribed = await transcribe_message_images(message, exclude_spec=main_spec)
        if transcribed is None:
            return None
        replacements[index] = transcribed

    if not replacements:
        return None
    messages = [replacements.get(index, message) for index, message in enumerate(request.messages)]
    return request.override(messages=messages)


def _fix_mistyped_read_file_paths(request: ModelRequest, response: ModelResponse) -> ModelResponse:
    """纠正 read_file 工具调用里被模型抄错的长随机文件名。

    长随机文件名是模型抄写易错点，抄错一个字符就会 404（实测
    ``9fc2ac55…_preview.png`` 被抄成 ``9fc2c554…``）。这里在工具执行前，
    把与消息文本中真实路径高度相似且无歧义的 file_path 纠正为真实路径；
    完全匹配、无接近路径或存在歧义时不动。
    """
    real_paths = collect_absolute_paths("\n".join(message_text(m) for m in request.messages))
    if not real_paths:
        return response

    changed = False
    result: list[Any] = []
    for message in response.result:
        calls = list(getattr(message, "tool_calls", None) or [])
        new_calls = []
        message_changed = False
        for call in calls:
            fixed = call
            if call.get("name") == "read_file":
                args = call.get("args") or {}
                path = args.get("file_path")
                if isinstance(path, str) and path not in real_paths:
                    best, _ratio, ambiguous = closest_real_path(path, real_paths)
                    if best and not ambiguous:
                        fixed = {**call, "args": {**args, "file_path": best}}
                        message_changed = True
                        logger.info(f"read_file 路径已自动纠正: {path} -> {best}")
            new_calls.append(fixed)
        if message_changed and new_calls != calls:
            message = message.model_copy(update={"tool_calls": new_calls})
            changed = True
        result.append(message)

    if not changed:
        return response
    return ModelResponse(result=result)


def _ocr_fallback_response(image_paths: list[str]) -> ModelResponse:
    if not image_paths:
        return ModelResponse(result=[AIMessage(content="当前模型无法读取图片，且没有可供 OCR 工具解析的文件路径。")])

    tool_calls = [
        {
            "name": "ocr_parse_file",
            "args": {"file_path": path},
            "id": f"call_ocr_{uuid4().hex}",
        }
        for path in image_paths
    ]
    return ModelResponse(
        result=[
            AIMessage(
                content="当前模型不支持图片输入，正在改用 OCR 工具提取图片文字。",
                tool_calls=tool_calls,
            )
        ]
    )


def _has_image(messages: list[Any]) -> bool:
    return any(
        isinstance(block, dict) and block.get("type") in {"image", "image_url", "input_image"}
        for message in messages
        for block in getattr(message, "content_blocks", [])
    )


def _is_image_input_rejection(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code not in {400, 404, 415, 422} and not isinstance(exc, ValueError):
        return False

    detail = str(exc).lower()
    # 供应商只说 content 项类型非法、不说明图片时，由调用方依据"本轮确有图片"判定
    if any(term in detail for term in _CONTENT_TYPE_REJECTION_TERMS):
        return True
    return any(term in detail for term in _IMAGE_ERROR_TERMS) and any(term in detail for term in _REJECTION_TERMS)
