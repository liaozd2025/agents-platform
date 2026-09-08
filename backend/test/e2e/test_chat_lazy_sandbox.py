"""真实 HTTP、worker 与模型探针：纯聊天不创建动态沙盒。"""

import asyncio
import os
import uuid
from time import monotonic

import httpx
import pytest
from sqlalchemy import delete, select

from e2e_helpers import cancel_run, iter_sse, wait_for_run
from test.live_api_cleanup import delete_test_conversation_resources
from yuxi.agents.backends.sandbox import sandbox_id_for_thread
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import (
    Agent,
    AgentRun,
    Conversation,
    Message,
    Project,
    Role,
    User,
    UserRoleAssignment,
)
from yuxi.utils.auth_utils import AuthUtils


@pytest.mark.asyncio
@pytest.mark.e2e
@pytest.mark.slow
async def test_plain_chat_completes_without_creating_sandbox(e2e_client):
    """使用隔离测试账号连续问候，核对 SSE 时的实例与最终消息。"""
    model = os.getenv("TEST_CHAT_MODEL")
    if not model:
        pytest.skip("真实模型探针需要 TEST_CHAT_MODEL")
    uid = f"pytest-lazy-{uuid.uuid4().hex}"
    thread_id = str(uuid.uuid4())
    password = uuid.uuid4().hex
    project_id = str(uuid.uuid4())
    workdir = f"projects/{project_id}"
    run_ids = []
    try:
        async with pg_manager.get_async_session_context() as db:
            role = await db.scalar(select(Role).where(Role.code == "user"))
            assert role is not None
            db.add(
                User(
                    uid=uid,
                    username=uid,
                    password_hash=AuthUtils.hash_password(password),
                    role_assignments=[UserRoleAssignment(role=role, scope_mode="inherit")],
                )
            )
            db.add(
                Agent(
                    slug=uid,
                    name="纯聊天探针",
                    backend_id="ChatbotAgent",
                    created_by=uid,
                    share_config={
                        "version": 2,
                        "read_scope": {"access_level": "user", "user_uids": [uid], "department_ids": []},
                        "manage_scope": None,
                    },
                    config_json={
                        "context": {
                            "model": model,
                            "system_prompt": "用户向你问好时简短回复即可。",
                            "tools": [],
                            "skills": [],
                            "mcps": [],
                            "subagents": [],
                            "knowledges": [],
                        }
                    },
                )
            )
            await db.flush()
            db.add(
                Project(
                    id=project_id, uid=uid, selection_status="implicit", workdir_path=workdir, directory_mode="managed"
                )
            )
            await db.flush()
            db.add(Conversation(thread_id=thread_id, uid=uid, agent_id=uid, project_id=project_id))
        from yuxi.workspace.paths import ensure_bound_user_workdir

        await asyncio.to_thread(ensure_bound_user_workdir, uid, workdir)
        login = await e2e_client.post("/api/auth/token", data={"username": uid, "password": password})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        sandbox_id = sandbox_id_for_thread(thread_id, uid=uid)
        async with httpx.AsyncClient(
            base_url=os.environ["SANDBOX_PROVISIONER_URL"],
            headers={"Authorization": f"Bearer {os.environ['SANDBOX_PROVISIONER_TOKEN']}"},
            timeout=10,
        ) as provisioner:
            for _ in range(2):
                started_at = monotonic()
                response = await e2e_client.post(
                    "/api/agent/runs",
                    headers=headers,
                    json={
                        "query": "你好",
                        "agent_slug": uid,
                        "thread_id": thread_id,
                        "meta": {"request_id": f"pytest-lazy-{uuid.uuid4().hex}"},
                    },
                )
                assert response.status_code == 200, response.text
                run_id = response.json()["run_id"]
                run_ids.append(run_id)
                events = []
                first_message_at = None
                end_at = None
                async for event, payload in iter_sse(e2e_client, headers, run_id):
                    events.append(event)
                    if event == "messages" and first_message_at is None:
                        first_message_at = monotonic()
                    instance = await provisioner.get(f"/api/sandboxes/{sandbox_id}")
                    assert instance.status_code == 404, "纯聊天期间创建了动态沙盒"
                    if event == "end":
                        end_at = monotonic()
                        assert payload["payload"]["status"] == "completed", payload
                assert "messages" in events and "end" in events
                async with pg_manager.get_async_session_context() as db:
                    run = await db.get(AgentRun, run_id)
                    assert run.status == "completed" and not run.runtime_cleanup_pending
                    message = await db.get(Message, run.output_message_id)
                    assert message.run_id == run_id and message.content.strip()
                print(
                    f"纯聊天探针通过：首正文 {first_message_at - started_at:.2f}s，"
                    f"正文到结束 {end_at - first_message_at:.2f}s，未发现沙盒"
                )
    finally:
        for run_id in run_ids:
            await cancel_run(e2e_client, headers, run_id)
            await wait_for_run(e2e_client, headers, run_id)
        await delete_test_conversation_resources({(uid, workdir): {project_id}}, {thread_id}, {project_id})
        async with pg_manager.get_async_session_context() as db:
            await db.execute(delete(Agent).where(Agent.slug == uid))
            user = await db.scalar(select(User).where(User.uid == uid))
            if user is not None:
                await db.delete(user)
        await pg_manager.close()
