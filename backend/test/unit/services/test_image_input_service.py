"""图片输入能力判定与转述的服务层单测。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from yuxi.services import image_input_service


@pytest.fixture(autouse=True)
def clear_caches():
    """清空进程内缓存，避免用例之间互相影响。"""
    image_input_service._non_image_model_specs.clear()
    image_input_service._vision_transcripts.clear()
    yield
    image_input_service._non_image_model_specs.clear()
    image_input_service._vision_transcripts.clear()


class _FakeModelCache:
    def __init__(
        self,
        modalities_by_spec: dict[str, list[str] | None],
        provider_type: str = "openai",
    ):
        self.modalities_by_spec = modalities_by_spec
        self.provider_type = provider_type

    def get_model_info(self, spec: str):
        if spec not in self.modalities_by_spec:
            return None
        return SimpleNamespace(input_modalities=self.modalities_by_spec[spec], provider_type=self.provider_type)


def test_declared_text_only_requires_a_non_empty_declaration(monkeypatch: pytest.MonkeyPatch) -> None:
    """只有"声明非空且不含 image"才算确定不支持图片；空声明是未知能力，不能预判。"""
    monkeypatch.setattr(
        image_input_service,
        "model_cache",
        _FakeModelCache(
            {
                "alibaba-cn:qwen3.7-max": ["text"],
                "alibaba-cn:qwen3.7-flash": ["text", "image"],
                "custom:undeclared": [],
                "custom:none-returned": None,
            }
        ),
    )

    assert image_input_service.model_is_declared_text_only("alibaba-cn:qwen3.7-max") is True
    assert image_input_service.model_is_declared_text_only("alibaba-cn:qwen3.7-flash") is False
    assert image_input_service.model_is_declared_text_only("custom:undeclared") is False
    assert image_input_service.model_is_declared_text_only("custom:none-returned") is False
    assert image_input_service.model_is_declared_text_only("custom:not-registered") is False
    assert image_input_service.model_is_declared_text_only(None) is False


@pytest.mark.asyncio
async def test_transcribe_skips_when_vision_model_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置视觉模型时必须返回 None 让调用方降级，不能伪造转述。"""

    class _Options:
        async def get(self):
            return {"vision_model": None}

    monkeypatch.setattr(image_input_service, "system_options", _Options())
    message = HumanMessage(
        content=[{"type": "text", "text": "看图"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]
    )

    assert await image_input_service.transcribe_message_images(message) is None


@pytest.mark.asyncio
async def test_transcribe_replaces_image_blocks_and_keeps_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """转述成功后图片块被替换为带来源标注的文本，原文与路径线索保留。"""

    class _Options:
        async def get(self):
            return {"vision_model": "alibaba-cn:qwen3.7-flash"}

    class _VisionModel:
        async def ainvoke(self, messages):
            return AIMessage(content="图中是一枚红色方块。")

    monkeypatch.setattr(image_input_service, "system_options", _Options())
    monkeypatch.setattr("yuxi.models.chat.load_chat_model", lambda _spec, **_kwargs: _VisionModel())
    message = HumanMessage(
        content=[
            {"type": "text", "text": "参考图路径：/home/gem/user-data/projects/p/uploads/a.png"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]
    )

    transcribed = await image_input_service.transcribe_message_images(
        message, exclude_spec="alibaba-cn:qwen3.7-max"
    )

    assert transcribed is not None
    assert [block["type"] for block in transcribed.content_blocks] == ["text", "text"]
    text = image_input_service.message_text(transcribed)
    assert "参考图路径：/home/gem/user-data/projects/p/uploads/a.png" in text
    assert '<image_transcript source="alibaba-cn:qwen3.7-flash">' in text
    assert "图中是一枚红色方块。" in text
    # 原消息不被就地修改
    assert [block["type"] for block in message.content_blocks] == ["text", "image"]


@pytest.mark.asyncio
async def test_transcribe_skips_when_vision_model_is_the_main_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """视觉模型与主模型相同时不得再调用同一模型。"""

    class _Options:
        async def get(self):
            return {"vision_model": "alibaba-cn:qwen3.7-max"}

    def fail_load(_spec, **_kwargs):
        raise AssertionError("视觉模型与主模型相同时不应加载模型")

    monkeypatch.setattr(image_input_service, "system_options", _Options())
    monkeypatch.setattr("yuxi.models.chat.load_chat_model", fail_load)
    message = HumanMessage(content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}])

    assert (
        await image_input_service.transcribe_message_images(message, exclude_spec="alibaba-cn:qwen3.7-max") is None
    )


def test_non_image_model_mark_expires() -> None:
    """"不接图"标记按 spec 记录，避免后续请求重复发注定失败的调用。"""
    assert image_input_service.is_known_non_image_model("alibaba-cn:qwen3.7-max") is False

    image_input_service.mark_non_image_model("alibaba-cn:qwen3.7-max")

    assert image_input_service.is_known_non_image_model("alibaba-cn:qwen3.7-max") is True
    assert image_input_service.is_known_non_image_model("alibaba-cn:qwen3.7-flash") is False


@pytest.mark.asyncio
async def test_tuning_params_only_apply_to_openai_compatible_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """限长/关思考只发给 OpenAI 兼容系；其他厂商直接朴素调用，不发注定被拒的参数。"""
    message = HumanMessage(content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}])

    for provider_type, expect_tuning in (("openai", True), ("anthropic", False), ("gemini", False)):
        monkeypatch.setattr(
            image_input_service,
            "model_cache",
            _FakeModelCache({"vision-vendor:m": ["text", "image"]}, provider_type=provider_type),
        )

        class _Options:
            async def get(self):
                return {"vision_model": "vision-vendor:m"}

        seen: dict = {}

        class _VisionModel:
            async def ainvoke(self, messages):
                return AIMessage(content="图中是一枚红色方块。")

        def fake_load(spec, **kwargs):
            seen["kwargs"] = kwargs
            return _VisionModel()

        monkeypatch.setattr(image_input_service, "system_options", _Options())
        monkeypatch.setattr("yuxi.models.chat.load_chat_model", fake_load)

        result = await image_input_service.transcribe_message_images(message, exclude_spec="main:m")

        assert result is not None
        if expect_tuning:
            assert seen["kwargs"]["max_tokens"] == image_input_service._TRANSCRIBE_MAX_TOKENS
            assert seen["kwargs"]["extra_body"] == {"enable_thinking": False}
        else:
            assert "max_tokens" not in seen["kwargs"]
            assert "extra_body" not in seen["kwargs"]
        image_input_service._vision_transcripts.clear()


def test_correct_transcript_paths_fixes_mistyped_hash() -> None:
    """转述里被抄错的 hash 路径自动纠正为消息原文中的真实路径（实测缺陷回放）。"""
    real = "/home/gem/user-data/projects/w1/uploads/9fc2ac5549a8434cb734e71009d6c16a_preview.png"
    transcript = f"这是一张品牌标识图片。\n**图片文件路径：**\n{real.replace('9fc2ac55', '9fc2c554')}"
    reference = f"<attachment_context>\n- preview.png: {real}\n</attachment_context>"

    corrected = image_input_service.correct_transcript_paths(transcript, reference)

    assert real in corrected
    assert "9fc2c554" not in corrected


def test_correct_transcript_paths_keeps_exact_and_skips_ambiguous() -> None:
    """完全匹配的路径不动；存在第二个同样接近的候选（歧义）时不纠正。"""
    a = "/home/gem/user-data/projects/w1/uploads/9fc2ac5549a8434cb734e71009d6c16a_preview.png"
    b = "/home/gem/user-data/projects/w1/uploads/9fc2ac5549a8434cb734e71009d6c16b_preview.png"
    reference = f"- {a}\n- {b}"

    # 完全匹配：原样保留
    assert image_input_service.correct_transcript_paths(f"路径 {a}", reference) == f"路径 {a}"

    # 候选与 a、b 都只差一个字符：歧义，不纠正
    ambiguous_candidate = a[:-1] + "x"
    assert (
        image_input_service.correct_transcript_paths(f"路径 {ambiguous_candidate}", reference)
        == f"路径 {ambiguous_candidate}"
    )


@pytest.mark.asyncio
async def test_transcribe_message_images_corrects_path_in_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端：视觉模型转述里抄错的路径在注入前被纠正。"""
    real = "/home/gem/user-data/projects/w1/uploads/9fc2ac5549a8434cb734e71009d6c16a_preview.png"

    class _Options:
        async def get(self):
            return {"vision_model": "alibaba-cn:qwen3.7-flash"}

    class _VisionModel:
        async def ainvoke(self, messages):
            garbled = real.replace("9fc2ac55", "9fc2c554")
            return AIMessage(content=f"图片描述。\n图片文件路径：\n{garbled}")

    monkeypatch.setattr(image_input_service, "system_options", _Options())
    monkeypatch.setattr(
        image_input_service,
        "model_cache",
        _FakeModelCache({"alibaba-cn:qwen3.7-flash": ["text", "image"]}),
    )
    monkeypatch.setattr("yuxi.models.chat.load_chat_model", lambda _spec, **_kwargs: _VisionModel())
    message = HumanMessage(
        content=[
            {"type": "text", "text": f"分析这个文件：<attachment_context>\n- preview.png: {real}\n</attachment_context>"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]
    )

    result = await image_input_service.transcribe_message_images(message, exclude_spec="alibaba-cn:qwen3.7-max")

    assert result is not None
    text = image_input_service.message_text(result)
    assert real in text
    assert "9fc2c554" not in text
