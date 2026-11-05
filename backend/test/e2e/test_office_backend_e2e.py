"""Office 工具通过当前 Session/Turn 协议产出可回读文件，不创建旧 PI 执行。"""

import io
from uuid import uuid4

import asyncpg
import pytest
from docx import Document

from test.e2e.e2e_helpers import (
    DETERMINISTIC_OUTPUT,
    archive_public_thread,
    create_lifecycle_agent,
    delete_agent,
    output_text,
    postgres_dsn,
    public_message,
    register_replay_provider,
    wait_for_consumed_input,
    wait_for_terminal_turn,
)
from test.live_api_cleanup import make_test_session_title
from yuxi.modules.workspace.paths import user_workspace_dir
from yuxi.modules.workspace.runtime_paths import WORKSPACE_PATH

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.timeout(240)]


async def test_office_parser_turn_preserves_source_and_publishes_real_markdown(e2e_client, e2e_headers, openai_replay_url):
    """真实入口、worker、数据库及文件回读证明 Office 解析，不靠模型自述完成。"""
    client, headers = e2e_client, e2e_headers
    provider_id = "ci-office-" + uuid4().hex[:8]
    await register_replay_provider(client, headers, base_url=openai_replay_url, provider_id=provider_id)
    slug = thread_id = turn_id = None
    connection = None
    try:
        profile = await client.get("/api/auth/me", headers=headers)
        assert profile.status_code == 200, profile.text
        uid = str(profile.json()["uid"])
        slug = await create_lifecycle_agent(client, headers, uid, tools=["ocr_parse_file"], model_spec=f"{provider_id}:deterministic-chat")
        created = await client.post(
            "/api/v1/agents/sessions",
            headers={**headers, "Idempotency-Key": uuid4().hex},
            json={"agent_id": slug, "title": make_test_session_title("office-parser")},
        )
        assert created.status_code == 201, created.text
        thread_id = created.json()["id"]
        connection = await asyncpg.connect(postgres_dsn())
        workdir = await connection.fetchval(
            "SELECT p.workdir_path FROM sessions s JOIN projects p ON p.id=s.project_id WHERE s.thread_id=$1 AND s.uid=$2",
            thread_id,
            uid,
        )
        assert workdir
        source = io.BytesIO()
        document = Document()
        document.add_paragraph("Office backend content 37")
        document.save(source)
        uploaded = await client.post(
            "/api/viewer/filesystem/upload",
            headers=headers,
            data={"thread_id": thread_id, "parent_path": "/"},
            files={
                "files": (
                    "office-parser.docx",
                    source.getvalue(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert uploaded.status_code == 200, uploaded.text
        original_url = uploaded.json()["entries"][0]["artifact_url"]
        prompt = f"{DETERMINISTIC_OUTPUT} DETERMINISTIC_OFFICE_PATH:{WORKSPACE_PATH}/{workdir}/office-parser.docx"
        accepted = await client.post(
            f"/api/v1/agents/sessions/{thread_id}/events",
            headers={**headers, "Idempotency-Key": uuid4().hex},
            json={"events": [{"type": "agent.session.input.message", "input": [public_message(prompt)]}]},
        )
        assert accepted.status_code == 202, accepted.text
        consumed = await wait_for_consumed_input(client, headers, accepted.json())
        turn_id = consumed["turn_id"]
        result = await wait_for_terminal_turn(client, headers, thread_id, turn_id, timeout=120)
        assert result["status"] == "completed", result
        assert output_text(result["yuxi"]["output"]) == DETERMINISTIC_OUTPUT
        run_id = result["yuxi"]["result_run_id"]
        assert await connection.fetchval("SELECT count(*) FROM agent_runs WHERE created_by_run_id=$1", run_id) == 0
        assert (
            await connection.fetchval(
                "SELECT execution_status FROM messages WHERE run_id=$1 AND message_type='tool_audit' AND operation_id='call-office-parser'",
                run_id,
            )
            == "completed"
        )
        root = user_workspace_dir(uid) / workdir
        [markdown] = list(root.glob("outputs/ocr/*/document.md"))
        assert "Office backend content 37" in markdown.read_text()
        artifact_path = f"{WORKSPACE_PATH}/{workdir}/{markdown.relative_to(root).as_posix()}"
        parsed = await client.get(f"/api/v1/agents/sessions/{thread_id}/artifacts/{artifact_path.lstrip('/')}", headers=headers)
        assert parsed.status_code == 200 and parsed.content == markdown.read_bytes()
        original = await client.get(original_url, headers=headers, params={"download": "true"})
        assert original.status_code == 200 and original.content == source.getvalue()
    finally:
        if connection is not None:
            await connection.close()
        if thread_id:
            await archive_public_thread(client, headers, thread_id, turn_id=turn_id)
        if slug:
            await delete_agent(client, headers, slug)
        response = await client.delete(f"/api/system/model-providers/{provider_id}", headers=headers)
        assert response.status_code in (200, 404), response.text
