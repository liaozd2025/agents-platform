"""真实 Local sandbox 中的 PI golden Task。"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest
from yuxi.services.pi_execution_service import (
    LocalPiAdapter,
    PiExecutionCancelled,
    build_default_pi_runtime_manifest,
    execute_pi_attempt,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.slow]


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
    artifact = adapter.output_path("pi-golden.txt")
    patch = adapter.output_path(final["payload"]["patch"]["path"])
    session = adapter.output_path(final["payload"]["session"]["path"])
    assert final["payload"]["text"] == "YUXI_PI_GOLDEN_V1"
    assert artifact.read_text(encoding="utf-8") == "YUXI_PI_GOLDEN_V1"
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == final["payload"]["artifact"]["sha256"]
    assert patch.read_text(encoding="utf-8") == (
        "--- /dev/null\n+++ b/pi-golden.txt\n@@ -0,0 +1 @@\n+YUXI_PI_GOLDEN_V1\n"
    )
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == final["payload"]["patch"]["sha256"]
    assert session.is_file()
    assert next(event for event in accepted.values() if event["type"] == "log")["payload"] == {"message": "pi_started"}
    assert await adapter.instance_exists() is False
    assert sorted(path.name for path in artifact.parent.parent.iterdir()) == ["outputs"]


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
    assert adapter.output_path("pi-golden.txt").parent.parent.exists() is False
