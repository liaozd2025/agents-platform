"""真实 Local sandbox 中的 PI golden Task。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from test.live_api_cleanup import (
    delete_test_conversation_resources,
    make_test_resource_id,
)
from yuxi.agents.buildin import agent_manager
from yuxi.agents.backends.sandbox import ProvisionerSandboxBackend
from yuxi.agents.backends.sandbox.provider import get_sandbox_provider
from yuxi.agents.middlewares.pi_sandbox import create_pi_sandbox_middleware
from yuxi.agents.skills.service import get_user_skills_root_dir
from yuxi.services import run_worker
from yuxi.services.agent_run_service import load_agent_run_result, stream_agent_run_events
from yuxi.services.pi_execution_service import (
    LocalPiAdapter,
    PI_RUNNER_PATH,
    PiExecutionCancelled,
    build_default_pi_runtime_manifest,
    build_pi_runtime_manifest,
    execute_pi_attempt,
    validate_pi_runtime,
)
from yuxi.services.run_queue_service import append_run_stream_event, get_redis_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Agent, AgentRun, Conversation, Message, Project, User
from yuxi.workspace.paths import ensure_bound_user_workdir

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.slow]


def _decode_sse_chunks(chunks: list[str]) -> list[tuple[str, dict]]:
    items = []
    for chunk in chunks:
        event = "message"
        data_lines = []
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event = line.removeprefix("event:").strip() or "message"
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").strip())
        if data_lines:
            items.append((event, json.loads("\n".join(data_lines))))
    return items


def _pi_tool_events(value) -> set[tuple[str, str]]:
    events: set[tuple[str, str]] = set()
    if isinstance(value, dict):
        stream_event = value.get("stream_event")
        if isinstance(stream_event, dict) and stream_event.get("type") == "tool_call":
            events.add(("call", str(stream_event.get("name") or "")))
        if value.get("event") == "tool-finished" and isinstance(value.get("output"), dict):
            events.add(("result", str(value["output"].get("name") or "")))
        for child in value.values():
            events.update(_pi_tool_events(child))
    elif isinstance(value, list):
        for child in value:
            events.update(_pi_tool_events(child))
    return events


async def test_pi_golden_result_survives_final_ack_and_rootfs_cleanup():
    run_id = f"pi-golden-{uuid.uuid4().hex}"
    manifest, digest = build_default_pi_runtime_manifest()
    adapter = LocalPiAdapter(uid="pi-e2e", run_id=run_id, attempt_id="1")
    accepted: dict[str, dict] = {}

    async def sink(envelope):
        previous = accepted.setdefault(envelope["event_id"], envelope)
        assert previous == envelope
        return {"ack": True, "duplicate": previous is not envelope}

    events = await execute_pi_attempt(
        attempt={
            "run_id": run_id,
            "attempt_id": "1",
            "manifest": manifest,
            "manifest_digest": digest,
        },
        adapter=adapter,
        result_sink=sink,
    )

    final = next(event for event in events if event["type"] == "final")
    artifact = adapter.read_output("pi-golden.txt")
    patch = adapter.read_output(final["payload"]["patch"]["path"])
    session = adapter.read_output(final["payload"]["session"]["path"])
    assert final["payload"]["text"] == "YUXI_PI_GOLDEN_V1"
    assert artifact.decode() == "YUXI_PI_GOLDEN_V1"
    assert hashlib.sha256(artifact).hexdigest() == final["payload"]["artifact"]["sha256"]
    assert patch.decode() == (
        "--- /dev/null\n+++ b/pi-golden.txt\n@@ -0,0 +1 @@\n+YUXI_PI_GOLDEN_V1\n\\ No newline at end of file\n"
    )
    assert hashlib.sha256(patch).hexdigest() == final["payload"]["patch"]["sha256"]
    assert session
    assert next(event for event in accepted.values() if event["type"] == "log")["payload"] == {"message": "pi_started"}
    tool_calls = [event for event in accepted.values() if event["type"] == "tool_call"]
    tool_results = [event for event in accepted.values() if event["type"] == "tool_result"]
    assert [event["payload"]["name"] for event in tool_calls] == ["read", "write"]
    assert [event["payload"]["tool_call_id"] for event in tool_results] == ["read-skill", "write-golden"]
    assert await adapter.instance_exists() is False


async def test_pi_cancel_removes_instance_and_attempt_scope():
    run_id = f"pi-cancel-{uuid.uuid4().hex}"
    manifest, digest = build_default_pi_runtime_manifest()
    adapter = LocalPiAdapter(uid="pi-e2e", run_id=run_id, attempt_id="1")
    cancel_event = asyncio.Event()
    cancel_event.set()

    with pytest.raises(PiExecutionCancelled, match="cancelled"):
        await execute_pi_attempt(
            attempt={
                "run_id": run_id,
                "attempt_id": "1",
                "manifest": manifest,
                "manifest_digest": digest,
            },
            adapter=adapter,
            result_sink=lambda _envelope: None,
            cancel_event=cancel_event,
        )

    assert await adapter.instance_exists() is False
    assert adapter.workdir_exists() is False


async def test_pi_stream_callback_cancel_removes_instance_and_attempt_scope():
    run_id = f"pi-stream-cancel-{uuid.uuid4().hex}"
    manifest, digest = build_default_pi_runtime_manifest()
    adapter = LocalPiAdapter(uid="pi-e2e", run_id=run_id, attempt_id="1")

    async def reject_first_event(_envelope):
        raise PiExecutionCancelled("cancelled by event sink")

    with pytest.raises(PiExecutionCancelled, match="event sink"):
        await execute_pi_attempt(
            attempt={
                "run_id": run_id,
                "attempt_id": "1",
                "manifest": manifest,
                "manifest_digest": digest,
            },
            adapter=adapter,
            result_sink=reject_first_event,
        )

    assert await adapter.instance_exists() is False
    assert adapter.workdir_exists() is False


async def test_pi_runtime_digest_matches_mixed_case_skill_tree(tmp_path):
    skill_dir = tmp_path / "mixed-case-skill"
    (skill_dir / "agents").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Mixed case\n", encoding="utf-8")
    (skill_dir / "agents" / "worker.md").write_text("worker\n", encoding="utf-8")
    run_id = f"pi-digest-{uuid.uuid4().hex}"
    manifest, _ = build_pi_runtime_manifest(
        runner_path=PI_RUNNER_PATH,
        skill_sources={"mixed-case-skill": skill_dir},
    )
    adapter = LocalPiAdapter(
        uid="pi-e2e",
        run_id=run_id,
        attempt_id="1",
        skill_sources={"mixed-case-skill": skill_dir},
    )
    instance_id = await adapter.create({"run_id": run_id, "attempt_id": "1", "manifest": manifest})

    try:
        validate_pi_runtime(manifest, await adapter.inspect(instance_id))
    finally:
        await adapter.stop(instance_id)


async def test_pi_runner_rejects_model_job_without_supported_authentication():
    run_id = f"pi-auth-{uuid.uuid4().hex}"
    model = {
        "provider_type": "openai",
        "model_id": "model-1",
        "display_name": "Model 1",
        "api": "openai-completions",
        "base_url": "https://example.com/v1",
        "context_window": 128_000,
        "max_tokens": 32_768,
        "sampling_params": {},
    }
    manifest, _ = build_default_pi_runtime_manifest(model=model)
    adapter = LocalPiAdapter(
        uid="pi-e2e",
        run_id=run_id,
        attempt_id="1",
        credentials={"headers": {}},
    )
    instance_id = await adapter.create({"run_id": run_id, "attempt_id": "1", "manifest": manifest})

    try:
        with pytest.raises(RuntimeError, match="credentials are missing or unsupported"):
            await adapter.execute(
                instance_id,
                {
                    "run_id": run_id,
                    "attempt_id": "1",
                    "manifest": manifest,
                    "task": "Do not execute this task.",
                },
            )
    finally:
        await adapter.stop(instance_id)


async def test_pi_sandbox_assembled_path_prioritizes_run_output_directory(
    monkeypatch,
):
    uid = f"pytest-pi-{uuid.uuid4().hex}"
    project_id = str(uuid.uuid4())
    workdir_path = f"projects/{project_id}"
    thread_id = f"pytest-pi-thread-{uuid.uuid4().hex}"
    parent_run_id = str(uuid.uuid4())
    request_id = make_test_resource_id("pi-sandbox-parent")
    child_thread_id = ""
    child_run_id = ""
    parent_backend = None
    projection_root = None

    try:
        async with pg_manager.get_async_session_context() as db:
            agent = await db.scalar(select(Agent).where(Agent.is_default.is_(True)))
            assert agent is not None
            agent_slug = agent.slug
            db.add(User(username=uid, uid=uid, password_hash="test"))
            await db.flush()
            db.add(
                Project(
                    id=project_id,
                    uid=uid,
                    selection_status="implicit",
                    workdir_path=workdir_path,
                    directory_mode="managed",
                )
            )
            await db.flush()
            conversation = Conversation(
                thread_id=thread_id,
                uid=uid,
                project_id=project_id,
                agent_id=agent_slug,
                title="pytest pi sandbox assembled",
                status="active",
                extra_metadata={"_yuxi_test": True, "_yuxi_e2e": True},
            )
            db.add(conversation)
            await db.flush()
            message = Message(
                conversation_id=conversation.id,
                role="user",
                content="通过 PI Agent 生成 golden 产物",
                request_id=request_id,
                delivery_status="dispatched",
            )
            db.add(message)
            await db.flush()
            run = AgentRun(
                id=parent_run_id,
                conversation_thread_id=thread_id,
                runtime_scope_id=thread_id,
                agent_slug=agent_slug,
                uid=uid,
                request_id=request_id,
                conversation_id=conversation.id,
                input_message_id=message.id,
                input_payload={"model_spec": "pytest:golden", "tool_approval_mode": "always_trust"},
                status="running",
                run_type="chat",
            )
            db.add(run)
            message.run_id = parent_run_id

        await asyncio.to_thread(ensure_bound_user_workdir, uid, workdir_path)
        projection_root = get_user_skills_root_dir(uid)
        parent_backend = ProvisionerSandboxBackend(
            thread_id=thread_id,
            uid=uid,
            inherit_env=True,
            create_if_missing=True,
            workdir_path=workdir_path,
        )
        await asyncio.to_thread(parent_backend.ensure_available)
        agent_manager.auto_discover_agents()
        monkeypatch.setattr(run_worker, "resolve_pi_model_runtime", lambda _model_spec: (None, {}))

        context = SimpleNamespace(
            uid=uid,
            run_id=parent_run_id,
            thread_id=thread_id,
            runtime_scope_id=thread_id,
            workdir_path=f"/home/gem/user-data/{workdir_path}",
            _effective_skill_slugs=[],
            _runtime_skills={},
        )
        legacy_output_path = f"/home/gem/user-data/{workdir_path}/outputs/pi-golden.txt"
        command = (
            await create_pi_sandbox_middleware(context)
            .tools[0]
            .coroutine(
                description=f"运行 PI golden 任务，将产物写入 {legacy_output_path}",
                runtime=SimpleNamespace(tool_call_id="call-pi-assembled"),
            )
        )
        child = command.update["subagent_runs"][0]
        child_run_id = child["run_id"]
        child_thread_id = child["child_thread_id"]
        assert child["status"] == "completed"

        result = await load_agent_run_result(run_id=child_run_id, current_uid=uid)
        assert result["status"] == "completed"
        assert result["output"] == "YUXI_PI_GOLDEN_V1"
        assert result["pi"]["artifact"]["path"] == "pi-golden.txt"

        async with pg_manager.get_async_session_context() as db:
            persisted_child = await db.get(AgentRun, child_run_id)
            assert persisted_child is not None
            assert persisted_child.status == "completed"
            assert persisted_child.run_type == "sandbox"
            assert persisted_child.created_by_run_id == parent_run_id

        await append_run_stream_event(parent_run_id, "end", {"status": "completed"}, thread_id=thread_id)
        sse_chunks = [
            chunk
            async for chunk in stream_agent_run_events(
                run_id=parent_run_id,
                after_seq="0-0",
                current_uid=uid,
            )
        ]
        parent_events = _decode_sse_chunks(sse_chunks)
        tool_events = _pi_tool_events([payload for _event, payload in parent_events])
        assert {("call", "read"), ("result", "read"), ("call", "write"), ("result", "write")} <= tool_events
    finally:
        if parent_backend is not None:
            parent_backend.close()
        if thread_id and workdir_path:
            await asyncio.to_thread(
                get_sandbox_provider().release,
                thread_id,
                uid=uid,
                workdir_path=workdir_path,
            )
        redis = await get_redis_client()
        stream_ids = [run_id for run_id in (parent_run_id, child_run_id) if run_id]
        if stream_ids:
            await redis.delete(*(f"run:events:{run_id}" for run_id in stream_ids))
        thread_ids = {value for value in (thread_id, child_thread_id) if value}
        await delete_test_conversation_resources(
            {(uid, workdir_path): {project_id}},
            thread_ids,
            {project_id},
        )
        async with pg_manager.get_async_session_context() as db:
            await db.execute(delete(User).where(User.uid == uid))
        if projection_root is not None:
            shutil.rmtree(projection_root)
