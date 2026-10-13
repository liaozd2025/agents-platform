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
        content=[
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]
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

    transcribed = await image_input_service.transcribe_message_images(message, exclude_spec="alibaba-cn:qwen3.7-max")

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

    assert await image_input_service.transcribe_message_images(message, exclude_spec="alibaba-cn:qwen3.7-max") is None


def test_non_image_model_mark_expires() -> None:
    """ "不接图"标记按 spec 记录，避免后续请求重复发注定失败的调用。"""
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


@pytest.mark.asyncio
async def test_transcript_cache_scopes_identity_prunes_expiry_and_limits_capacity(monkeypatch):
    """真实调用次数证明身份隔离、过期回收与固定容量。"""

    class Options:
        async def get(self):
            return {"vision_model": "vendor:vision"}

    calls = []
    loads = []

    class Vision:
        async def ainvoke(self, messages):
            calls.append(messages)
            return AIMessage("描述")

    def load(spec, **kwargs):
        loads.append(kwargs)
        return Vision()

    monkeypatch.setattr(image_input_service, "system_options", Options())
    monkeypatch.setattr("yuxi.models.chat.load_chat_model", load)
    monkeypatch.setattr(image_input_service, "_VISION_CACHE_MAX_ENTRIES", 2)
    message = HumanMessage(content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}])
    for uid, thread in [("u1", "t1"), ("u1", "t1"), ("u2", "t1"), ("u2", "t2")]:
        assert await image_input_service.transcribe_message_images(message, uid=uid, session_id=thread)
        assert loads[-1]["uid"] == uid and loads[-1]["session_id"] == thread
    assert len(calls) == 3 and len(image_input_service._vision_transcripts) == 2
    for key in image_input_service._vision_transcripts:
        image_input_service._vision_transcripts[key] = (0, "过期")
    await image_input_service.transcribe_message_images(message, uid="u3", session_id="t3")
    assert len(calls) == 4 and len(image_input_service._vision_transcripts) == 1


def test_explicit_image_declaration_overrides_learned_rejection(monkeypatch):
    """管理员更新声明后立即恢复图片直传；明确支持的声明不得被学习覆盖。"""
    cache = _FakeModelCache({"vendor:main": []})
    monkeypatch.setattr(image_input_service, "model_cache", cache)
    image_input_service.mark_non_image_model("vendor:main")
    assert image_input_service.is_known_non_image_model("vendor:main")
    cache.modalities_by_spec["vendor:main"] = ["text", "image"]
    assert not image_input_service.is_known_non_image_model("vendor:main")
    image_input_service._non_image_model_specs.clear()
    image_input_service.mark_non_image_model("vendor:main")
    assert not image_input_service._non_image_model_specs


def test_image_fingerprint_preserves_block_boundaries():
    """有序图片列表的边界与 MIME 不得在缓存键中丢失。"""
    left = [{"url": "https://a.test/ahttps://b.test/c"}, {"url": "https://d.test/e"}]
    right = [{"url": "https://a.test/a"}, {"url": "https://b.test/chttps://d.test/e"}]
    assert image_input_service._image_fingerprint(left) != image_input_service._image_fingerprint(right)
    assert image_input_service._image_fingerprint(left) != image_input_service._image_fingerprint(left[::-1])
    assert image_input_service._image_fingerprint(
        [{"base64": "AA", "mime_type": "image/png"}]
    ) != image_input_service._image_fingerprint([{"base64": "AA", "mime_type": "image/jpeg"}])


def test_image_fingerprint_ignores_generated_block_ids():
    """LangChain 自动生成的块 ID 不得破坏同一图片的缓存命中。"""
    first = {"base64": "AA", "mime_type": "image/png", "id": "random-a", "index": 0}
    second = {**first, "id": "random-b", "index": 1}
    assert image_input_service._image_fingerprint([first]) == image_input_service._image_fingerprint([second])
