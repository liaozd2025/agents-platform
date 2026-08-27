"""PI 执行 seam 的最小合同测试。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yuxi.services.pi_execution_service as pi_execution_service
from yuxi.services.agent_run_manifest_service import canonical_json
from yuxi.services.pi_execution_service import (
    PI_NODE_VERSION,
    PI_PACKAGE_INTEGRITY,
    PI_PACKAGE_VERSION,
    LocalPiAdapter,
    PiCleanupFailed,
    PiExecutionCancelled,
    PiExecutionUnknown,
    PiRuntimeMismatch,
    build_pi_runtime_manifest,
    execute_pi_attempt,
)


class FakeAdapter:
    name = "local"

    def __init__(self, actual_runtime: dict, events: list[dict] | None = None):
        self.actual_runtime = actual_runtime
        self.events = events or []
        self.execute_calls = 0
        self.stop_calls = 0
        self.stop_preserve_outputs: list[bool] = []
        self.validated_refs: list[dict] = []

    async def create(self, _attempt):
        return "sandbox-1"

    async def inspect(self, _instance_id):
        return self.actual_runtime

    async def execute(self, _instance_id, _job):
        self.execute_calls += 1
        return self.events

    async def validate_ref(self, ref):
        self.validated_refs.append(ref)
        if ref.get("sha256") == "invalid":
            raise ValueError("invalid ref")

    async def stop(self, _instance_id, *, preserve_outputs=False):
        self.stop_calls += 1
        self.stop_preserve_outputs.append(preserve_outputs)


def _skill_digest(skill_dir: Path) -> str:
    hasher = hashlib.sha256()
    for path in sorted(item for item in skill_dir.rglob("*") if item.is_file()):
        hasher.update(path.relative_to(skill_dir).as_posix().encode())
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _manifest(tmp_path: Path) -> tuple[dict, str, dict]:
    skill_dir = tmp_path / "pi-golden"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("golden", encoding="utf-8")
    runner = tmp_path / "runner.mjs"
    runner.write_text("export {};", encoding="utf-8")
    manifest, digest = build_pi_runtime_manifest(runner_path=runner, skill_dir=skill_dir)
    actual = {
        "runner_protocol": manifest["runner"]["protocol"],
        "runner_digest": manifest["runner"]["digest"],
        "pi_version": PI_PACKAGE_VERSION,
        "pi_integrity": PI_PACKAGE_INTEGRITY,
        "node_version": PI_NODE_VERSION,
        "skills": {"pi-golden": _skill_digest(skill_dir)},
        "skill_bundle": manifest["skill_bundle"],
    }
    return manifest, digest, actual


@pytest.mark.asyncio
async def test_manifest_mismatch_stops_before_pi_execute(tmp_path):
    manifest, digest, actual = _manifest(tmp_path)
    actual["node_version"] = "22.20.0"
    adapter = FakeAdapter(actual)

    with pytest.raises(PiRuntimeMismatch, match="node_version"):
        await execute_pi_attempt(
            attempt={"run_id": "run-1", "attempt_id": "1", "manifest": manifest, "manifest_digest": digest},
            adapter=adapter,
            result_sink=lambda _envelope: None,
        )

    assert adapter.execute_calls == 0
    assert adapter.stop_calls == 1
    assert adapter.stop_preserve_outputs == [False]


@pytest.mark.asyncio
async def test_bundle_digest_mismatch_stops_before_pi_execute(tmp_path):
    manifest, digest, actual = _manifest(tmp_path)
    actual["skill_bundle"] = {**actual["skill_bundle"], "digest": "0" * 64}
    adapter = FakeAdapter(actual)

    with pytest.raises(PiRuntimeMismatch, match="skill_bundle"):
        await execute_pi_attempt(
            attempt={"run_id": "run-1", "attempt_id": "1", "manifest": manifest, "manifest_digest": digest},
            adapter=adapter,
            result_sink=lambda _envelope: None,
        )

    assert adapter.execute_calls == 0
    assert adapter.stop_preserve_outputs == [False]


@pytest.mark.asyncio
async def test_success_wraps_replayable_envelopes_and_stops_after_final_ack(tmp_path):
    manifest, digest, actual = _manifest(tmp_path)
    raw_final = {
        "event_id": "final-1",
        "sequence": 4,
        "type": "final",
        "payload": {
            "text": "YUXI_PI_GOLDEN_V1",
            "artifact": {"path": "pi-golden.txt", "sha256": "a" * 64},
            "patch": {"path": "pi-golden.patch", "sha256": "b" * 64},
            "session": {"path": "pi-session/session.jsonl", "sha256": "c" * 64},
        },
    }
    adapter = FakeAdapter(
        actual,
        events=[
            {"event_id": "log-1", "sequence": 0, "type": "log", "payload": {"message": "started"}},
            {
                "event_id": "artifact-1",
                "sequence": 1,
                "type": "artifact",
                "ref": raw_final["payload"]["artifact"],
            },
            {"event_id": "patch-1", "sequence": 2, "type": "patch", "ref": raw_final["payload"]["patch"]},
            {"event_id": "session-1", "sequence": 3, "type": "session", "ref": raw_final["payload"]["session"]},
            raw_final,
            raw_final,
        ],
    )
    accepted: dict[str, dict] = {}

    async def sink(envelope):
        previous = accepted.setdefault(envelope["event_id"], envelope)
        assert previous == envelope
        return {"ack": True, "duplicate": previous is not envelope}

    events = await execute_pi_attempt(
        attempt={
            "run_id": "run-1",
            "attempt_id": "1",
            "manifest": manifest,
            "manifest_digest": digest,
        },
        adapter=adapter,
        result_sink=sink,
    )

    assert len(events) == 6
    assert list(accepted) == ["log-1", "artifact-1", "patch-1", "session-1", "final-1"]
    assert accepted["final-1"] == {
        "job_id": "run-1",
        "attempt_id": "1",
        "adapter": "local",
        "event_id": "final-1",
        "sequence": 4,
        "type": "final",
        "runtime_manifest_digest": digest,
        "payload": raw_final["payload"],
        "payload_digest": hashlib.sha256(canonical_json(raw_final["payload"]).encode()).hexdigest(),
    }
    assert adapter.validated_refs == [
        raw_final["payload"]["artifact"],
        raw_final["payload"]["patch"],
        raw_final["payload"]["session"],
        raw_final["payload"]["artifact"],
        raw_final["payload"]["patch"],
        raw_final["payload"]["session"],
        raw_final["payload"]["artifact"],
        raw_final["payload"]["patch"],
        raw_final["payload"]["session"],
    ]
    assert adapter.stop_calls == 1
    assert adapter.stop_preserve_outputs == [True]


@pytest.mark.asyncio
async def test_lost_final_ack_replays_same_envelope_and_preserves_outputs(tmp_path):
    manifest, digest, actual = _manifest(tmp_path)
    refs = {
        "artifact": {"path": "pi-golden.txt", "sha256": "a" * 64},
        "patch": {"path": "pi-golden.patch", "sha256": "b" * 64},
        "session": {"path": "pi-session/session.jsonl", "sha256": "c" * 64},
    }
    events = [
        {"event_id": f"{kind}-1", "sequence": sequence, "type": kind, "ref": ref}
        for sequence, (kind, ref) in enumerate(refs.items())
    ]
    events.append(
        {
            "event_id": "final-1",
            "sequence": 3,
            "type": "final",
            "payload": {"text": "YUXI_PI_GOLDEN_V1", **refs},
        }
    )
    adapter = FakeAdapter(actual, events=events)
    final_calls: list[dict] = []

    async def sink(envelope):
        if envelope["type"] != "final":
            return {"ack": True, "duplicate": False}
        final_calls.append(envelope)
        if len(final_calls) == 1:
            raise TimeoutError("ACK response lost after commit")
        return {"ack": True, "duplicate": True}

    result = await execute_pi_attempt(
        attempt={"run_id": "run-1", "attempt_id": "1", "manifest": manifest, "manifest_digest": digest},
        adapter=adapter,
        result_sink=sink,
    )

    assert result == events
    assert len(final_calls) == 2
    assert final_calls[0] == final_calls[1]
    assert adapter.stop_preserve_outputs == [True]


@pytest.mark.asyncio
async def test_started_execution_failure_is_unknown_and_not_replayable(tmp_path):
    manifest, digest, actual = _manifest(tmp_path)
    adapter = FakeAdapter(actual)

    async def fail_after_start(_instance_id, _job):
        adapter.execute_calls += 1
        raise TimeoutError("transport lost")

    adapter.execute = fail_after_start

    with pytest.raises(PiExecutionUnknown, match="transport lost"):
        await execute_pi_attempt(
            attempt={"run_id": "run-1", "attempt_id": "1", "manifest": manifest, "manifest_digest": digest},
            adapter=adapter,
            result_sink=lambda _envelope: None,
        )

    assert adapter.execute_calls == 1
    assert adapter.stop_preserve_outputs == [True]


@pytest.mark.asyncio
async def test_invalid_server_ref_is_rejected_before_result_sink_ack(tmp_path):
    manifest, digest, actual = _manifest(tmp_path)
    adapter = FakeAdapter(
        actual,
        events=[
            {
                "event_id": "artifact-1",
                "sequence": 0,
                "type": "artifact",
                "ref": {"path": "missing.txt", "sha256": "invalid"},
            }
        ],
    )
    sink_calls = 0

    async def sink(_envelope):
        nonlocal sink_calls
        sink_calls += 1

    with pytest.raises(PiExecutionUnknown, match="invalid ref"):
        await execute_pi_attempt(
            attempt={"run_id": "run-1", "attempt_id": "1", "manifest": manifest, "manifest_digest": digest},
            adapter=adapter,
            result_sink=sink,
        )

    assert sink_calls == 0
    assert adapter.stop_preserve_outputs == [True]


@pytest.mark.asyncio
async def test_cleanup_failure_is_explicit_and_never_reports_final_ack(tmp_path):
    manifest, digest, actual = _manifest(tmp_path)
    adapter = FakeAdapter(actual)

    async def fail_stop(_instance_id, *, preserve_outputs=False):
        adapter.stop_calls += 1
        adapter.stop_preserve_outputs.append(preserve_outputs)
        raise TimeoutError("delete unavailable")

    adapter.stop = fail_stop

    with pytest.raises(PiCleanupFailed, match="delete unavailable") as error:
        await execute_pi_attempt(
            attempt={"run_id": "run-1", "attempt_id": "1", "manifest": manifest, "manifest_digest": digest},
            adapter=adapter,
            result_sink=lambda _envelope: None,
        )

    assert adapter.stop_calls == 1
    assert adapter.stop_preserve_outputs == [True]
    assert isinstance(error.value.primary, PiExecutionUnknown)


@pytest.mark.asyncio
async def test_cancel_with_cleanup_failure_preserves_cancel_primary(tmp_path):
    manifest, digest, actual = _manifest(tmp_path)
    adapter = FakeAdapter(actual)
    cancel_event = asyncio.Event()
    cancel_event.set()

    async def fail_stop(_instance_id, *, preserve_outputs=False):
        adapter.stop_preserve_outputs.append(preserve_outputs)
        raise TimeoutError("delete unavailable")

    adapter.stop = fail_stop

    with pytest.raises(PiCleanupFailed) as error:
        await execute_pi_attempt(
            attempt={"run_id": "run-1", "attempt_id": "1", "manifest": manifest, "manifest_digest": digest},
            adapter=adapter,
            result_sink=lambda _envelope: None,
            cancel_event=cancel_event,
        )

    assert isinstance(error.value.primary, PiExecutionCancelled)
    assert adapter.stop_preserve_outputs == [False]


@pytest.mark.asyncio
async def test_preflight_and_cleanup_failure_keeps_bound_instance_and_primary(tmp_path):
    manifest, digest, actual = _manifest(tmp_path)
    adapter = FakeAdapter(actual)
    bound_instances: list[str] = []

    async def fail_inspect(_instance_id):
        raise RuntimeError("preflight unavailable")

    async def fail_stop(_instance_id, *, preserve_outputs=False):
        adapter.stop_preserve_outputs.append(preserve_outputs)
        raise TimeoutError("delete unavailable")

    adapter.inspect = fail_inspect
    adapter.stop = fail_stop

    with pytest.raises(PiCleanupFailed) as error:
        await execute_pi_attempt(
            attempt={"run_id": "run-1", "attempt_id": "1", "manifest": manifest, "manifest_digest": digest},
            adapter=adapter,
            result_sink=lambda _envelope: None,
            instance_sink=bound_instances.append,
        )

    assert bound_instances == ["sandbox-1"]
    assert isinstance(error.value.primary, RuntimeError)
    assert str(error.value.primary) == "preflight unavailable"
    assert adapter.stop_preserve_outputs == [False]


@pytest.mark.asyncio
async def test_cancel_stops_blocked_execution_once(tmp_path):
    manifest, digest, actual = _manifest(tmp_path)
    cancel_event = asyncio.Event()
    adapter = FakeAdapter(actual)

    async def wait_forever(_instance_id, _job):
        adapter.execute_calls += 1
        await asyncio.Event().wait()

    adapter.execute = wait_forever
    task = asyncio.create_task(
        execute_pi_attempt(
            attempt={
                "run_id": "run-1",
                "attempt_id": "1",
                "manifest": manifest,
                "manifest_digest": digest,
            },
            adapter=adapter,
            result_sink=lambda _envelope: None,
            cancel_event=cancel_event,
        )
    )
    await asyncio.sleep(0)
    cancel_event.set()

    with pytest.raises(PiExecutionCancelled):
        await task

    assert adapter.execute_calls == 1
    assert adapter.stop_calls == 1
    assert adapter.stop_preserve_outputs == [False]


@pytest.mark.asyncio
async def test_cancel_between_events_prevents_final_ack(tmp_path):
    manifest, digest, actual = _manifest(tmp_path)
    cancel_event = asyncio.Event()
    adapter = FakeAdapter(
        actual,
        events=[
            {"event_id": "log-1", "sequence": 0, "type": "log", "payload": {"message": "started"}},
            {
                "event_id": "final-1",
                "sequence": 1,
                "type": "final",
                "payload": {"text": "YUXI_PI_GOLDEN_V1"},
            },
        ],
    )
    accepted = []

    async def sink(envelope):
        accepted.append(envelope["event_id"])
        cancel_event.set()
        return {"ack": True, "duplicate": False}

    with pytest.raises(PiExecutionCancelled):
        await execute_pi_attempt(
            attempt={
                "run_id": "run-1",
                "attempt_id": "1",
                "manifest": manifest,
                "manifest_digest": digest,
            },
            adapter=adapter,
            result_sink=sink,
            cancel_event=cancel_event,
        )

    assert accepted == ["log-1"]
    assert adapter.stop_calls == 1
    assert adapter.stop_preserve_outputs == [False]


@pytest.mark.asyncio
async def test_local_ref_rejects_path_escape_symlink_and_wrong_digest(tmp_path, monkeypatch):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    artifact = outputs / "artifact.txt"
    artifact.write_text("ok", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (outputs / "escape").symlink_to(outside)
    adapter = LocalPiAdapter.__new__(LocalPiAdapter)
    adapter._scope = "scope"
    monkeypatch.setattr(pi_execution_service, "sandbox_outputs_dir", lambda _scope: outputs)

    with pytest.raises(ValueError, match="安全相对路径"):
        adapter.output_path("/absolute.txt")
    with pytest.raises(ValueError, match="安全相对路径"):
        adapter.output_path("../outside.txt")
    with pytest.raises(ValueError, match="逃逸"):
        adapter.output_path("escape")
    with pytest.raises(ValueError, match="摘要不匹配"):
        await adapter.validate_ref({"path": "artifact.txt", "sha256": "0" * 64})


@pytest.mark.asyncio
async def test_local_stop_retries_release_and_keeps_only_acked_outputs(tmp_path, monkeypatch):
    root = tmp_path / "scope"
    outputs = root / "outputs"
    (root / "skills").mkdir(parents=True)
    (root / "uploads").mkdir()
    outputs.mkdir()
    (outputs / "result.txt").write_text("ok", encoding="utf-8")

    class Provider:
        calls = 0

        def release(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("transient delete failure")

    adapter = LocalPiAdapter.__new__(LocalPiAdapter)
    adapter._scope = "scope"
    adapter._uid = "user-1"
    adapter._backend = SimpleNamespace(id="sandbox-1")
    adapter._provider = Provider()
    adapter._stopped = False
    monkeypatch.setattr(pi_execution_service, "sandbox_user_data_dir", lambda _scope: root)
    monkeypatch.setattr(pi_execution_service, "sandbox_outputs_dir", lambda _scope: outputs)

    await adapter.stop("sandbox-1", preserve_outputs=True)
    await adapter.stop("sandbox-1", preserve_outputs=True)

    assert adapter._provider.calls == 2
    assert [path.name for path in root.iterdir()] == ["outputs"]
    assert (outputs / "result.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_local_stop_removes_cancelled_attempt_scope(tmp_path, monkeypatch):
    root = tmp_path / "scope"
    outputs = root / "outputs"
    (root / "skills").mkdir(parents=True)
    outputs.mkdir()

    class Provider:
        def release(self, *_args, **_kwargs):
            return None

    adapter = LocalPiAdapter.__new__(LocalPiAdapter)
    adapter._scope = "scope"
    adapter._uid = "user-1"
    adapter._backend = SimpleNamespace(id="sandbox-1")
    adapter._provider = Provider()
    adapter._stopped = False
    monkeypatch.setattr(pi_execution_service, "sandbox_user_data_dir", lambda _scope: root)
    monkeypatch.setattr(pi_execution_service, "sandbox_outputs_dir", lambda _scope: outputs)

    await adapter.stop("sandbox-1", preserve_outputs=False)

    assert root.exists() is False
