"""PI wire 在真实 PostgreSQL JSONB 边界的转换与拒绝分类。"""

import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from yuxi.services.agent_run_manifest_service import canonical_json
from yuxi.services.pi_execution_service import (
    PiResultPersistenceFailed,
    build_default_pi_runtime_manifest,
    build_pi_envelope,
    execute_pi_attempt,
)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pi_nul_roundtrips_jsonb_and_deterministic_rejection_stops_once():
    """原始 NUL 确实被数据库拒绝，清理后回读一致，其他数据错误不重试。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    manifest, digest = build_default_pi_runtime_manifest()
    attempt = {"run_id": "synthetic-run", "attempt_id": "1", "manifest": manifest, "manifest_digest": digest}
    event = {"event_id": "tool-1", "sequence": 0, "type": "tool_result", "payload": {"content": "before\x00after"}}
    try:
        async with engine.connect() as db:
            with pytest.raises(DBAPIError) as invalid:
                await db.execute(text("SELECT CAST(:value AS JSONB)"), {"value": json.dumps(event)})
            assert invalid.value.orig.sqlstate == "22P05"
            await db.rollback()

            envelope = build_pi_envelope(attempt=attempt, adapter_name="local", event=event)
            await db.execute(text("CREATE TEMP TABLE pi_envelope_check (value JSONB)"))
            await db.execute(
                text("INSERT INTO pi_envelope_check VALUES (CAST(:value AS JSONB))"), {"value": json.dumps(envelope)}
            )
            await db.commit()
            persisted = (await db.execute(text("SELECT value FROM pi_envelope_check"))).scalar_one()
            assert persisted == envelope
            assert persisted["payload"]["content"] == "before\\u0000after"
            assert (
                persisted["payload_digest"] == hashlib.sha256(canonical_json(persisted["payload"]).encode()).hexdigest()
            )

        actual = {
            "runner_protocol": manifest["runner"]["protocol"],
            "runner_digest": manifest["runner"]["digest"],
            "pi_version": manifest["pi"]["version"],
            "pi_integrity": manifest["pi"]["integrity"],
            "node_version": manifest["node"]["version"],
            "skills": {item["path"]: item["digest"] for item in manifest["skill_bundle"]["items"]},
        }
        adapter = SimpleNamespace(
            name="local",
            create=AsyncMock(return_value="sandbox"),
            inspect=AsyncMock(return_value=actual),
            execute=AsyncMock(return_value=[event]),
            stop=AsyncMock(),
        )
        submitted = []

        async def reject_invalid_data(value):
            """在真实数据库触发确定性数据转换失败。"""
            submitted.append(value)
            async with engine.begin() as db:
                await db.execute(text("SELECT CAST(CAST(:invalid AS text) AS integer)"), {"invalid": "not-an-integer"})

        with pytest.raises(PiResultPersistenceFailed) as failure:
            await execute_pi_attempt(attempt=attempt, adapter=adapter, result_sink=reject_invalid_data)
        assert isinstance(failure.value.__cause__, DBAPIError)
        assert failure.value.__cause__.orig.sqlstate == "22P02"
        assert len(submitted) == 1
        adapter.stop.assert_awaited_once_with("sandbox", preserve_outputs=True)
    finally:
        await engine.dispose()
