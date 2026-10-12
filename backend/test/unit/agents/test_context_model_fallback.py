"""模型下架后运行时上下文的模型回落行为。

背景：模型被供应商下架后不在模型缓存里，但会话绑定的 model_spec、系统默认
default_model 仍可能指向它。历史缺陷是这两处都不校验可用性，导致
`GET /api/chat/thread/{id}/state` 抛 Unknown model spec 返回 500，用户连历史会话都打不开。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxi.agents import context as agent_context


class _FakeModelCache:
    """最小模型缓存桩：只提供本模块用到的两个查询方法。"""

    def __init__(self, specs: dict[str, str]) -> None:
        # spec -> model_type
        self._specs = specs

    def get_model_info(self, spec: str):
        model_type = self._specs.get(spec)
        if model_type is None:
            return None
        return SimpleNamespace(spec=spec, model_type=model_type)

    def get_all_specs(self, model_type: str | None = None):
        return [
            SimpleNamespace(spec=spec, mt=mt)
            for spec, mt in self._specs.items()
            if model_type is None or mt == model_type
        ]


@pytest.fixture
def fake_cache(monkeypatch: pytest.MonkeyPatch):
    """把模型缓存替换成可控桩，避免依赖真实 Redis / 数据库。"""
    holder = {"cache": _FakeModelCache({})}

    def _install(specs: dict[str, str]) -> _FakeModelCache:
        cache = _FakeModelCache(specs)
        holder["cache"] = cache
        import yuxi.models.providers.cache as cache_module

        monkeypatch.setattr(cache_module, "model_cache", cache)
        return cache

    return _install


def test_empty_model_spec_is_not_resolvable(fake_cache):
    fake_cache({})
    assert agent_context._is_model_spec_resolvable("") is False
    assert agent_context._is_model_spec_resolvable(None) is False
    assert agent_context._is_model_spec_resolvable("   ") is False


def test_available_chat_model_is_resolvable(fake_cache):
    fake_cache({"alibaba-cn:qwen3.7-plus": "chat"})
    assert agent_context._is_model_spec_resolvable("alibaba-cn:qwen3.7-plus") is True
    # 前后空白应被容忍，与既有 spec 处理方式一致
    assert agent_context._is_model_spec_resolvable("  alibaba-cn:qwen3.7-plus  ") is True


def test_delisted_model_is_not_resolvable(fake_cache):
    """下架模型的缓存里查不到 → 判定为不可解析（这是本次修复的核心）。"""
    fake_cache({"alibaba-cn:qwen3.7-plus": "chat"})
    assert agent_context._is_model_spec_resolvable("alibaba-cn:qwen3.7-max") is False


def test_non_chat_model_is_not_resolvable(fake_cache):
    """embedding / rerank 放进对话上下文没有意义，同样视为不可解析。"""
    fake_cache(
        {
            "alibaba-cn:text-embedding-v4": "embedding",
            "alibaba-cn:qwen3-rerank": "rerank",
        }
    )
    assert agent_context._is_model_spec_resolvable("alibaba-cn:text-embedding-v4") is False
    assert agent_context._is_model_spec_resolvable("alibaba-cn:qwen3-rerank") is False


@pytest.mark.asyncio
async def test_available_default_model_is_returned_as_is(fake_cache, monkeypatch: pytest.MonkeyPatch):
    """默认模型本身可用时原样返回，不做任何多余回落。"""
    fake_cache({"alibaba-cn:qwen3.7-max": "chat"})

    async def fake_options_get(*_args, **_kwargs):
        return {"default_model": "alibaba-cn:qwen3.7-max"}

    # system_options 是 frozen dataclass 实例，不能直接 setattr；按仓库既有做法替换其类型上的方法
    monkeypatch.setattr(type(agent_context.system_options), "get", fake_options_get)

    assert await agent_context.resolve_available_default_chat_model() == "alibaba-cn:qwen3.7-max"


@pytest.mark.asyncio
async def test_delisted_default_model_falls_back_to_first_available(
    fake_cache, monkeypatch: pytest.MonkeyPatch
):
    """系统默认对话模型被下架时，回落到可用列表首项，而不是把失效 spec 继续传下去。"""
    fake_cache(
        {
            "alibaba-cn:qwen3.7-plus": "chat",
            "alibaba-cn:qwen3.7-flash": "chat",
            "alibaba-cn:text-embedding-v4": "embedding",
        }
    )

    async def fake_options_get(*_args, **_kwargs):
        return {"default_model": "alibaba-cn:qwen3.7-max"}  # 已被下架

    # system_options 是 frozen dataclass 实例，不能直接 setattr；按仓库既有做法替换其类型上的方法
    monkeypatch.setattr(type(agent_context.system_options), "get", fake_options_get)

    resolved = await agent_context.resolve_available_default_chat_model()
    # 必须仍是可解析的对话模型，且不包含 embedding
    assert resolved == "alibaba-cn:qwen3.7-plus"
    assert agent_context._is_model_spec_resolvable(resolved) is True


@pytest.mark.asyncio
async def test_no_available_chat_model_returns_empty(fake_cache, monkeypatch: pytest.MonkeyPatch):
    """一个可用对话模型都没有时返回空串，由调用方决定失败方式，不塞入非法值。"""
    fake_cache({"alibaba-cn:text-embedding-v4": "embedding"})

    async def fake_options_get(*_args, **_kwargs):
        return {"default_model": "alibaba-cn:qwen3.7-max"}

    # system_options 是 frozen dataclass 实例，不能直接 setattr；按仓库既有做法替换其类型上的方法
    monkeypatch.setattr(type(agent_context.system_options), "get", fake_options_get)

    assert await agent_context.resolve_available_default_chat_model() == ""


@pytest.mark.asyncio
async def test_fallback_skips_non_chat_entries(fake_cache, monkeypatch: pytest.MonkeyPatch):
    """列表里若只有非对话模型，不得把它当作对话默认模型返回。"""
    fake_cache({"alibaba-cn:text-embedding-v4": "embedding", "alibaba-cn:qwen3-rerank": "rerank"})

    async def fake_options_get(*_args, **_kwargs):
        return {"default_model": "alibaba-cn:qwen3.7-max"}

    # system_options 是 frozen dataclass 实例，不能直接 setattr；按仓库既有做法替换其类型上的方法
    monkeypatch.setattr(type(agent_context.system_options), "get", fake_options_get)

    assert await agent_context.resolve_available_default_chat_model() == ""


def test_chat_service_ignores_delisted_conversation_model():
    """读取会话状态时，已下架的会话绑定模型必须被忽略。

    历史缺陷：`GET /api/chat/thread/{id}/state` 把会话绑定的 model_spec 原样交给模型
    解析层，模型下架后抛 Unknown model spec，整个接口 500，用户连历史会话都打不开。

    这里用源码断言而非 import chat_service：后者的导入链会连带 middlewares 等一整套
    模块，任何一处环境缺失都会让本用例以「导入失败」而非「行为回归」告终，掩盖真实信号。
    """
    import inspect
    import pathlib

    import yuxi.agents as agents_pkg

    # services 是命名空间包（__file__ 可能为 None），从同包的 agents 模块所在目录上溯定位
    source_path = pathlib.Path(agents_pkg.__file__).parent.parent / "services" / "chat_service.py"
    source = source_path.read_text(encoding="utf-8")

    # 必须提供可解析性判定，并覆盖会话绑定与子智能体继承两条来源
    assert "def _is_model_spec_usable(" in source
    assert '_is_model_spec_usable(conversation_model_spec, "会话绑定")' in source
    assert '_is_model_spec_usable(model_spec, "子智能体继承")' in source
    # 不得退回「只判类型和空值」的原始写法
    assert 'if isinstance(conversation_model_spec, str) and conversation_model_spec.strip():' not in source


def test_prepare_agent_runtime_context_falls_back_when_configured_model_delisted():
    """锁死调用点：运行时上下文必须在模型下架时回落到可用模型。

    只测辅助函数不够——把调用点退回「只判空」的原始写法时辅助函数测试仍会全绿，
    但真实行为已经回归成：下架模型继续传给下游 → load_chat_model 抛
    Unknown model spec → 会话状态接口 500。这里直接对调用点做源码断言。
    """
    import inspect

    source = inspect.getsource(agent_context.prepare_agent_runtime_context)
    # 必须走可解析性判定，而不是只判空
    assert "_is_model_spec_resolvable" in source
    assert "resolve_available_default_chat_model" in source
    # 不得退回只判空的原始写法
    assert 'if not str(getattr(context, "model", "") or "").strip()' not in source
