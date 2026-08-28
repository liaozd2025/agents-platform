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
    resolve_pi_model_runtime,
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


def _model_info(*, api_key: str = "", headers: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        model_type="chat",
        provider_type="openai",
        model_id="model-1",
        display_name="Model 1",
        api_key=api_key,
        base_url="https://example.com/v1",
        headers=headers or {},
        extra={},
        request_body_overrides={},
    )


def test_pi_model_runtime_rejects_missing_authentication(monkeypatch):
    monkeypatch.setattr(pi_execution_service.model_cache, "get_model_info", lambda _spec: _model_info())

    with pytest.raises(ValueError, match="缺少 API key"):
        resolve_pi_model_runtime("provider:model-1")


def test_pi_model_runtime_accepts_explicit_authentication_header(monkeypatch):
    info = _model_info(headers={"Authorization": "Bearer gateway-token"})
    monkeypatch.setattr(pi_execution_service.model_cache, "get_model_info", lambda _spec: info)

    _, credentials = resolve_pi_model_runtime("provider:model-1")

    assert credentials == {
        "api_key": "",
        "headers": info.headers,
        "auth_header": False,
    }


def _skill_digest(skill_dir: Path) -> str:
    hasher = hashlib.sha256()
    for path in sorted(skill_dir.rglob("*"), key=lambda item: item.relative_to(skill_dir).as_posix()):
        hasher.update(path.relative_to(skill_dir).as_posix().encode())
        hasher.update(b"\0")
        if path.is_dir():
            hasher.update(b"directory\0")
            continue
        if not path.is_file():
            hasher.update(b"other\0")
            continue
        hasher.update(b"file\0")
        hasher.update(bytes([path.stat().st_mode & 0o111]))
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
        "skills": {manifest["skill_bundle"]["items"][0]["path"]: _skill_digest(skill_dir)},
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
    actual["skills"][manifest["skill_bundle"]["items"][0]["path"]] = "0" * 64
    adapter = FakeAdapter(actual)

    with pytest.raises(PiRuntimeMismatch, match="skill pi-golden"):
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
async def test_final_ack_wins_over_concurrent_cancel_and_preserves_outputs(tmp_path):
    manifest, digest, actual = _manifest(tmp_path)
    cancel_event = asyncio.Event()
    refs = {
        "artifact": {"path": "artifact.json", "sha256": "a" * 64},
        "patch": {"path": "output.patch", "sha256": "b" * 64},
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
            "payload": {"text": "done", **refs},
        }
    )

    class StreamingAdapter(FakeAdapter):
        streams_events = True

        async def execute(self, _instance_id, _job, *, event_sink):
            self.execute_calls += 1
            for event in self.events:
                await event_sink(event)
            await asyncio.sleep(0.01)
            return self.events

    adapter = StreamingAdapter(actual, events)

    async def sink(envelope):
        if envelope["type"] == "final":
            cancel_event.set()
        return {"ack": True, "duplicate": False}

    result = await execute_pi_attempt(
        attempt={"run_id": "run-1", "attempt_id": "1", "manifest": manifest, "manifest_digest": digest},
        adapter=adapter,
        result_sink=sink,
        cancel_event=cancel_event,
    )

    assert result == events
    assert adapter.stop_preserve_outputs == [True]


@pytest.mark.asyncio
async def test_local_create_uses_attempt_isolated_workdir_and_skill_projection(monkeypatch):
    workdir = object()
    calls: dict[str, object] = {}

    class Backend:
        id = "sandbox-1"

        def __init__(self, thread_id: str, **kwargs):
            calls["backend"] = (thread_id, kwargs)

    class WorkdirFactory:
        @staticmethod
        def open_existing(uid: str, path: str):
            calls["open_workdir"] = (uid, path)
            return workdir

    monkeypatch.setattr(pi_execution_service, "get_sandbox_provider", object)
    monkeypatch.setattr(
        pi_execution_service,
        "ensure_bound_user_workdir",
        lambda uid, path: calls.setdefault("workdir", (uid, path)),
    )
    monkeypatch.setattr(pi_execution_service, "Workdir", WorkdirFactory)
    monkeypatch.setattr(
        pi_execution_service,
        "sync_user_accessible_skills",
        lambda uid, sources: calls.setdefault("skills", (uid, sources)),
    )
    monkeypatch.setattr(pi_execution_service, "ProvisionerSandboxBackend", Backend)

    adapter = LocalPiAdapter(uid="user-1", run_id="run-1", attempt_id="7")
    instance_id = await adapter.create({"run_id": "run-1", "attempt_id": "7"})

    runtime_uid, workdir_path = calls["workdir"]
    assert instance_id == "sandbox-1"
    assert runtime_uid == adapter._scope != "user-1"
    assert workdir_path.startswith("projects/")
    assert calls["open_workdir"] == (runtime_uid, workdir_path)
    assert adapter._workdir is workdir
    assert calls["skills"] == (runtime_uid, {"pi-golden": pi_execution_service.PI_GOLDEN_SKILL_DIR})
    assert calls["backend"] == (
        adapter._scope,
        {
            "uid": runtime_uid,
            "inherit_env": False,
            "create_if_missing": True,
            "workdir_path": workdir_path,
        },
    )


@pytest.mark.asyncio
async def test_local_create_failure_cleans_partial_workdir_and_skill_projection(monkeypatch):
    projection_calls: list[tuple[str, dict]] = []

    class WorkdirInstance:
        cleanup_calls: list[frozenset[str]] = []

        def cleanup(self, *, preserve_directories=frozenset()):
            self.cleanup_calls.append(preserve_directories)

    workdir = WorkdirInstance()

    class WorkdirFactory:
        @staticmethod
        def open_existing(_uid: str, _path: str):
            return workdir

    def sync(uid: str, sources: dict):
        projection_calls.append((uid, sources))
        if sources:
            raise RuntimeError("projection failed")

    monkeypatch.setattr(pi_execution_service, "get_sandbox_provider", object)
    monkeypatch.setattr(pi_execution_service, "ensure_bound_user_workdir", lambda _uid, _path: None)
    monkeypatch.setattr(pi_execution_service, "Workdir", WorkdirFactory)
    monkeypatch.setattr(pi_execution_service, "sync_user_accessible_skills", sync)

    adapter = LocalPiAdapter(uid="user-1", run_id="run-1", attempt_id="7")

    with pytest.raises(RuntimeError, match="projection failed"):
        await adapter.create({"run_id": "run-1", "attempt_id": "7"})

    assert workdir.cleanup_calls == [frozenset()]
    assert projection_calls == [
        (adapter._runtime_uid, {"pi-golden": pi_execution_service.PI_GOLDEN_SKILL_DIR}),
        (adapter._runtime_uid, {}),
    ]


@pytest.mark.asyncio
async def test_local_model_job_deletes_ephemeral_secret_when_runner_start_fails():
    calls: dict[str, object] = {}

    class Backend:
        id = "sandbox-1"

        def write_ephemeral_secret(self, path: str, _content: str) -> None:
            calls["written"] = path

        def delete_ephemeral_secret(self, path: str) -> None:
            calls["deleted"] = path

        async def aexecute_stream(self, _command, _sink, **kwargs):
            calls["stream"] = kwargs
            raise RuntimeError("runner launch failed")

    adapter = LocalPiAdapter.__new__(LocalPiAdapter)
    adapter._backend = Backend()
    adapter._stopped = False
    adapter._output_subdir = "pi-runs/0123456789abcdef01234567"
    adapter._credentials = {"api_key": "temporary"}
    adapter._run_id = "run-1"
    adapter._attempt_id = "1"

    with pytest.raises(RuntimeError, match="runner launch failed"):
        await adapter.execute(
            "sandbox-1",
            {
                "manifest": {
                    "model": {"model_id": "model-1"},
                    "policy": {"timeout_seconds": 60},
                }
            },
        )

    assert calls["written"] == calls["deleted"]
    assert calls["stream"] == {
        "timeout": 60,
        "max_output_bytes": pi_execution_service.PI_MAX_EVENT_STREAM_BYTES,
    }


@pytest.mark.asyncio
async def test_local_ref_rejects_path_escape_symlink_and_wrong_digest():
    class Workdir:
        def stat(self, path: str):
            if path.endswith("/escape"):
                raise PermissionError("symlink paths are not allowed")
            return {"is_dir": False, "size": 2}

        def read_file(self, path: str, max_bytes: int):
            assert path == "/outputs/pi-runs/0123456789abcdef01234567/artifact.txt"
            assert max_bytes == 2
            return b"ok"

    adapter = LocalPiAdapter.__new__(LocalPiAdapter)
    adapter._scope = "scope"
    adapter._runtime_uid = "scope"
    adapter._workdir_path = "projects/11111111-1111-4111-8111-111111111111"
    adapter._workdir = Workdir()
    adapter._output_subdir = "pi-runs/0123456789abcdef01234567"
    adapter._credentials = {}

    with pytest.raises(ValueError, match="安全相对路径"):
        adapter.read_output("/absolute.txt")
    with pytest.raises(ValueError, match="安全相对路径"):
        adapter.read_output("../outside.txt")
    with pytest.raises(PermissionError, match="symlink"):
        adapter.read_output("escape")
    with pytest.raises(ValueError, match="摘要不匹配"):
        await adapter.validate_ref({"path": "artifact.txt", "sha256": "0" * 64})


@pytest.mark.asyncio
async def test_local_stop_retries_release_and_keeps_only_acked_outputs(monkeypatch):
    class Provider:
        calls = 0

        def release(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("transient delete failure")

    class Workdir:
        cleanup_calls: list[frozenset[str]] = []

        def cleanup(self, *, preserve_directories=frozenset()):
            self.cleanup_calls.append(preserve_directories)
            return False

    adapter = LocalPiAdapter.__new__(LocalPiAdapter)
    adapter._scope = "scope"
    adapter._runtime_uid = "scope"
    adapter._workdir_path = "projects/11111111-1111-4111-8111-111111111111"
    adapter._workdir = Workdir()
    adapter._backend = SimpleNamespace(id="sandbox-1", close=lambda: None)
    adapter._provider = Provider()
    adapter._stopped = False
    adapter._reuse_sandbox = False
    projection_calls = []
    monkeypatch.setattr(
        pi_execution_service,
        "sync_user_accessible_skills",
        lambda uid, sources: projection_calls.append((uid, sources)),
    )

    await adapter.stop("sandbox-1", preserve_outputs=True)
    await adapter.stop("sandbox-1", preserve_outputs=True)

    assert adapter._provider.calls == 2
    assert adapter._workdir.cleanup_calls == [frozenset({"outputs"})]
    assert projection_calls == [(adapter._runtime_uid, {})]


@pytest.mark.asyncio
async def test_local_stop_keeps_bind_data_when_release_cannot_be_confirmed(monkeypatch):
    """实例可能仍存活时不得先删 bind-mounted Workdir 或 Skill 投影。"""

    class Provider:
        calls = 0

        def release(self, *_args, **_kwargs):
            self.calls += 1
            raise TimeoutError("delete unavailable")

    adapter = LocalPiAdapter.__new__(LocalPiAdapter)
    adapter._scope = "scope"
    adapter._runtime_uid = "scope"
    adapter._workdir_path = "projects/11111111-1111-4111-8111-111111111111"
    adapter._workdir = SimpleNamespace(cleanup=lambda **_kwargs: pytest.fail("must not cleanup workdir"))
    adapter._backend = SimpleNamespace(id="sandbox-1", close=lambda: None)
    adapter._provider = Provider()
    adapter._stopped = False
    adapter._reuse_sandbox = False
    monkeypatch.setattr(
        pi_execution_service,
        "sync_user_accessible_skills",
        lambda *_args, **_kwargs: pytest.fail("must not cleanup projection"),
    )

    with pytest.raises(TimeoutError, match="delete unavailable"):
        await adapter.stop("sandbox-1", preserve_outputs=True)

    assert adapter._provider.calls == 2


@pytest.mark.asyncio
async def test_local_stop_removes_cancelled_attempt_scope(monkeypatch):
    class Provider:
        def release(self, *_args, **_kwargs):
            return None

    class Workdir:
        cleanup_calls: list[frozenset[str]] = []

        def cleanup(self, *, preserve_directories=frozenset()):
            self.cleanup_calls.append(preserve_directories)
            return True

    adapter = LocalPiAdapter.__new__(LocalPiAdapter)
    adapter._scope = "scope"
    adapter._runtime_uid = "scope"
    adapter._workdir_path = "projects/11111111-1111-4111-8111-111111111111"
    adapter._workdir = Workdir()
    adapter._backend = SimpleNamespace(id="sandbox-1", close=lambda: None)
    adapter._provider = Provider()
    adapter._stopped = False
    adapter._reuse_sandbox = False
    projection_calls = []
    monkeypatch.setattr(
        pi_execution_service,
        "sync_user_accessible_skills",
        lambda uid, sources: projection_calls.append((uid, sources)),
    )

    await adapter.stop("sandbox-1", preserve_outputs=False)

    assert adapter._workdir.cleanup_calls == [frozenset()]
    assert projection_calls == [(adapter._runtime_uid, {})]


@pytest.mark.asyncio
async def test_local_child_adapter_reuses_parent_sandbox_without_releasing_it(monkeypatch):
    calls: dict[str, object] = {}

    class Backend:
        id = "sandbox-parent"

        def __init__(self, thread_id: str, **kwargs):
            calls["backend"] = (thread_id, kwargs)

        def ensure_available(self):
            calls["ensured"] = True

        def close(self):
            calls["closed"] = True

    workdir = SimpleNamespace(delete=lambda path: calls.setdefault("deleted", path))

    class WorkdirFactory:
        @staticmethod
        def open_existing(uid: str, path: str):
            calls["workdir"] = (uid, path)
            return workdir

    class Provider:
        def release(self, *_args, **_kwargs):
            raise AssertionError("PI child must not release the parent sandbox")

    monkeypatch.setattr(pi_execution_service, "ProvisionerSandboxBackend", Backend)
    monkeypatch.setattr(pi_execution_service, "Workdir", WorkdirFactory)
    monkeypatch.setattr(pi_execution_service, "get_sandbox_provider", Provider)
    adapter = LocalPiAdapter(
        uid="user-1",
        run_id="run-1",
        attempt_id="7",
        runtime_scope_id="root-thread",
        workdir_path="projects/11111111-1111-4111-8111-111111111111",
        skill_sources={},
        reuse_sandbox=True,
    )

    instance_id = await adapter.create(
        {
            "run_id": "run-1",
            "attempt_id": "7",
            "manifest": {"skill_bundle": {"items": []}},
        }
    )
    await adapter.stop(instance_id, preserve_outputs=True)

    assert calls["backend"] == (
        "root-thread",
        {
            "uid": "user-1",
            "inherit_env": True,
            "create_if_missing": False,
            "workdir_path": "projects/11111111-1111-4111-8111-111111111111",
        },
    )
    assert calls["ensured"] is True
    assert calls["closed"] is True
    assert "deleted" not in calls
