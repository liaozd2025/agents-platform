"""LLM 图谱抽取器 model_timeout 配置的校验测试。"""

from __future__ import annotations

import pytest

from yuxi.knowledge.graphs.extractors.llm import (
    DEFAULT_EXTRACTION_TIMEOUT_SECONDS,
    LLMGraphExtractor,
)

pytestmark = [pytest.mark.unit]


def test_model_timeout_falls_back_to_default_when_absent() -> None:
    """缺省时使用 300 秒默认超时。"""

    extractor = LLMGraphExtractor({"model_spec": "test/model"})

    assert extractor._resolve_timeout_seconds() == DEFAULT_EXTRACTION_TIMEOUT_SECONDS


@pytest.mark.parametrize("raw_timeout", ["abc", {}, []])
def test_model_timeout_rejects_non_numeric(raw_timeout) -> None:
    """非数字配置必须在选项校验阶段显式失败，不静默回退。"""

    extractor = LLMGraphExtractor({"model_spec": "test/model", "model_timeout": raw_timeout})

    with pytest.raises(ValueError, match="model_timeout 必须是数字"):
        extractor.validate_options()


@pytest.mark.parametrize("raw_timeout", [0, -1, "0", "-30"])
def test_model_timeout_rejects_non_positive(raw_timeout) -> None:
    """0 与负数会让单次抽取立即超时，必须显式失败。"""

    extractor = LLMGraphExtractor({"model_spec": "test/model", "model_timeout": raw_timeout})

    with pytest.raises(ValueError, match="model_timeout 必须大于 0"):
        extractor.validate_options()


def test_model_timeout_accepts_numeric_override() -> None:
    """合法数字配置被采纳，字符串数字同样可用。"""

    extractor = LLMGraphExtractor({"model_spec": "test/model", "model_timeout": "120.5"})

    assert extractor._resolve_timeout_seconds() == 120.5
