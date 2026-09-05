"""PI 上下文快照与逐模型能力的边界回归。"""

import hashlib
from copy import deepcopy
from dataclasses import replace

import pytest

from yuxi.models.providers.cache import ModelInfo
from yuxi.models.providers.service import _normalize_model_item
from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.services import pi_execution_service as pi
from yuxi.workspace import filesystem
from yuxi.workspace.workdir import Workdir


@pytest.fixture
def project(tmp_path, monkeypatch):
    """提供真实 no-follow Workdir。"""
    relative = "projects/11111111-1111-4111-8111-111111111111"
    root = tmp_path / relative
    root.mkdir(parents=True)
    monkeypatch.setattr(filesystem, "user_workspace_dir", lambda _uid: tmp_path)
    return Workdir.open_existing("u1", relative), root


def test_context_snapshot_is_bounded_and_rejects_symlinks_and_changed_session(project):
    workdir, root = project
    (root / "AGENTS.md").write_text("Authorized instruction", encoding="utf-8")
    source = root / "outputs/pi-runs/previous/pi-session/history.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"session bytes")
    previous = {
        "output_subdir": "pi-runs/previous",
        "ref": {"path": "pi-session/history.jsonl", "sha256": hashlib.sha256(b"session bytes").hexdigest()},
    }
    context, session = pi.snapshot_pi_context(workdir, previous)
    assert context["project_instructions"][0]["content"] == "Authorized instruction"
    assert session == {"content": "session bytes", "sha256": previous["ref"]["sha256"]}
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="摘要不匹配"):
        pi.snapshot_pi_context(workdir, previous)
    (root / "AGENTS.md").unlink()
    (root / "AGENTS.md").symlink_to(source)
    with pytest.raises(PermissionError, match="symlink"):
        pi.snapshot_pi_context(workdir, None)
    (root / "AGENTS.md").unlink()
    (root / "AGENTS.md").write_bytes(b"x" * (65536 + 1))
    with pytest.raises(ValueError, match="transfer limit"):
        pi.snapshot_pi_context(workdir, None)


@pytest.mark.parametrize(
    "field,value",
    [
        ("reasoning", "true"),
        ("context_length", True),
        ("max_completion_tokens", -1),
        ("input_modalities", "image"),
        ("input_modalities", [{}]),
        ("input_modalities", ["unknown"]),
    ],
)
def test_model_capabilities_reject_invalid_values(field, value):
    with pytest.raises(ValueError):
        _normalize_model_item({"id": "m", "type": "chat", field: value})


def test_pi_uses_exact_model_capabilities_and_conservative_unknowns(monkeypatch):
    info = ModelInfo(
        provider_id="p",
        model_id="m",
        model_type="chat",
        display_name="M",
        api_key="test",
        provider_type="openai",
        base_url="http://127.0.0.1/v1",
        context_length=32000,
        max_completion_tokens=8192,
        input_modalities=["text", "image", "audio"],
        reasoning=True,
        extra={"context_window": 100000, "max_tokens": 25000, "reasoning": False},
    )
    monkeypatch.setattr(pi.model_cache, "get_model_info", lambda _spec: info)
    model, _ = pi.resolve_pi_model_runtime("p:m")
    assert (model["context_window"], model["max_tokens"], model["input"], model["reasoning"]) == (
        32000,
        8192,
        ["text", "image"],
        True,
    )
    info = replace(
        info, context_length=None, max_completion_tokens=None, input_modalities=None, reasoning=None, extra={}
    )
    unknown, _ = pi.resolve_pi_model_runtime("p:m")
    assert (unknown["context_window"], unknown["max_tokens"], unknown["input"], unknown["reasoning"]) == (
        128000,
        32768,
        ["text"],
        None,
    )


@pytest.mark.parametrize(
    "mutation", ["wrong_model", "missing_count", "false_complete", "mismatched_total", "fake_zero"]
)
def test_pi_final_usage_rejects_untrusted_counts_and_model_attribution(mutation):
    total = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
    usage = {
        "schema_version": 2,
        "model_call_count": 1,
        "usage_reported_call_count": 1,
        "usage_unavailable_call_count": 0,
        "complete": True,
        "total": total,
        "models": {"p:m": {"model_call_count": 1, "usage_reported_call_count": 1, "usage": dict(total)}},
    }
    valid = deepcopy(usage)
    if mutation == "wrong_model":
        usage["models"]["p:other"] = usage["models"].pop("p:m")
    elif mutation == "missing_count":
        usage.pop("model_call_count")
    elif mutation == "false_complete":
        usage["complete"] = False
    elif mutation == "mismatched_total":
        usage["total"]["total_tokens"] = 99
    else:
        usage["total"] = {key: 0 for key in total}
        usage["models"]["p:m"]["usage"] = dict(usage["total"])
    with pytest.raises(ValueError, match="PI token_usage"):
        AgentRunRepository._pi_token_usage(usage, {"model": {"spec": "p:m"}})
    assert AgentRunRepository._pi_token_usage(valid, {"model": {"spec": "p:m"}}) == valid
