"""真实 HTTP、worker 与 PostgreSQL 验证共享撤销；通过同目录 run_execution_revocation.sh 创建并自动销毁专用环境。"""

import asyncio
import os
import uuid
from types import SimpleNamespace

import asyncpg
import httpx
import pytest

from yuxi.agents.backends.knowledge_base_backend import resolve_visible_knowledge_bases_for_context
from yuxi.agents.toolkits.kbs.tools import search_file
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Role, RolePermission, User, UserRoleAssignment
from yuxi.storage.postgres.models_knowledge import KnowledgeBase, KnowledgeFile
from yuxi.utils.auth_utils import AuthUtils


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_running_tool_rejects_committed_share_revocation():
    """模型屏障后撤权，旧运行、旧 Context 与新 HTTP 访问均拒绝文件元数据。"""
    base_url = os.getenv("TEST_KB_BASE_URL")
    model_url = os.getenv("TEST_KB_MODEL_URL")
    if not base_url or not model_url:
        pytest.skip("需要独立 API/worker/PG 和 revocation_model.py；设置 TEST_KB_BASE_URL/TEST_KB_MODEL_URL")
    # 仅在专用空库中运行：初始化失败意味着目标不是本测试的一次性环境。
    suffix = uuid.uuid4().hex[:10]
    admin_uid, reader_uid = f"kbadmin_{suffix}", f"kbreader_{suffix}"
    password = uuid.uuid4().hex
    kb_id, slug, provider = f"kb-{suffix}", f"kb-agent-{suffix}", f"kb-model-{suffix}"
    pg_manager.initialize()
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    async with (
        httpx.AsyncClient(base_url=base_url, timeout=30) as api,
        httpx.AsyncClient(base_url=model_url, timeout=10) as model,
    ):
        try:
            assert await conn.fetchval("SELECT count(*) FROM users") == 0, "测试需要一次性空库"
            response = await api.post("/api/auth/initialize", json={"uid": admin_uid, "password": password})
            assert response.status_code == 200, "仅允许在尚未初始化用户的隔离环境运行"
            response = await api.post("/api/auth/token", data={"username": admin_uid, "password": password})
            assert response.status_code == 200
            admin = {"Authorization": "Bearer " + response.json()["access_token"]}
            share = {
                "version": 2,
                "read_scope": {"access_level": "user", "user_uids": [reader_uid]},
                "manage_scope": None,
            }
            async with pg_manager.get_async_session_context() as db:
                role = Role(code=f"kb-reader-{suffix}", name="合成读者", default_scope_type="self")
                role.permissions = [RolePermission(permission_key=key) for key in ["knowledge_base:read", "skill:use"]]
                reader = User(uid=reader_uid, username=reader_uid, password_hash=AuthUtils.hash_password(password))
                db.add_all([role, reader])
                await db.flush()
                db.add(UserRoleAssignment(user_id=reader.id, role_id=role.id, scope_mode="inherit"))
                db.add(
                    KnowledgeBase(kb_id=kb_id, name=kb_id, kb_type="milvus", created_by=admin_uid, share_config=share)
                )
                await db.flush()
                db.add(
                    KnowledgeFile(
                        file_id=f"file-{suffix}",
                        kb_id=kb_id,
                        filename="revocation-sentinel.txt",
                        file_type="txt",
                        status="done",
                        file_size=12,
                    )
                )
            response = await api.post("/api/auth/token", data={"username": reader_uid, "password": password})
            assert response.status_code == 200
            reader_headers = {"Authorization": "Bearer " + response.json()["access_token"]}
            runtime = SimpleNamespace(context=SimpleNamespace(uid=reader_uid, knowledges=[kb_id]))
            await resolve_visible_knowledge_bases_for_context(runtime.context)
            assert "revocation-sentinel.txt" in str(
                await search_file.coroutine(query="revocation-sentinel", runtime=runtime)
            )
            response = await api.post(
                "/api/system/model-providers",
                headers=admin,
                json={
                    "provider_id": provider,
                    "display_name": "撤权测试模型",
                    "provider_type": "openai",
                    "base_url": model_url + "/v1",
                    "api_key": "synthetic-test-only",
                    "capabilities": ["chat"],
                    "enabled_models": [
                        {"id": "revocation", "display_name": "revocation", "type": "chat", "source": "manual"}
                    ],
                    "is_enabled": True,
                },
            )
            assert response.status_code == 200
            response = await api.post(
                "/api/agent",
                headers=admin,
                json={
                    "name": slug,
                    "slug": slug,
                    "backend_id": "ChatbotAgent",
                    "share_config": share,
                    "config_json": {
                        "context": {
                            "model": provider + ":revocation",
                            "system_prompt": "确定性撤权测试",
                            "tools": [],
                            "knowledges": [kb_id],
                            "mcps": [],
                            "skills": ["knowledge-base"],
                            "preload_skills": ["knowledge-base"],
                            "subagents": [],
                        }
                    },
                },
            )
            assert response.status_code == 200
            response = await api.post(
                "/api/chat/thread", headers=reader_headers, json={"agent_id": slug, "title": "共享撤销测试"}
            )
            assert response.status_code == 200
            thread = response.json()["id"]
            await model.post("/control/reset", json={})
            response = await api.post(
                "/api/agent/runs",
                headers=reader_headers,
                json={
                    "agent_slug": slug,
                    "thread_id": thread,
                    "query": "search revocation-sentinel",
                    "meta": {"request_id": str(uuid.uuid4())},
                },
            )
            assert response.status_code == 200
            run_id = response.json()["run_id"]
            for _ in range(200):
                control = (await model.get("/control")).json()
                if control["ready"]:
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("worker 未到达模型屏障")
            assert control["has_search_tool"]
            assert await conn.fetchval("SELECT status FROM agent_runs WHERE id=$1", run_id) == "running"
            response = await api.put(
                f"/api/knowledge/databases/{kb_id}",
                headers=admin,
                json={
                    "name": kb_id,
                    "description": "合成撤权数据",
                    "share_config": {
                        "version": 2,
                        "read_scope": {"access_level": "user", "user_uids": [admin_uid]},
                        "manage_scope": None,
                    },
                },
            )
            assert response.status_code == 200
            assert reader_uid not in await conn.fetchval(
                "SELECT share_config::text FROM knowledge_bases WHERE kb_id=$1", kb_id
            )
            assert (await api.get(f"/api/knowledge/databases/{kb_id}", headers=reader_headers)).status_code == 404
            await model.post("/control/release", json={})
            for _ in range(200):
                row = await conn.fetchrow(
                    "SELECT r.status,m.run_id,m.content FROM agent_runs r "
                    "LEFT JOIN messages m ON m.id=r.output_message_id WHERE r.id=$1",
                    run_id,
                )
                if row["status"] in {"completed", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("worker 未完成")
            assert row["status"] == "completed" and row["run_id"] == run_id
            for _ in range(200):
                attempts = await conn.fetch(
                    "SELECT outcome,cleanup_error,finished_at FROM agent_run_attempts WHERE run_id=$1", run_id
                )
                cleanup_pending = await conn.fetchval(
                    "SELECT runtime_cleanup_pending FROM agent_runs WHERE id=$1", run_id
                )
                if len(attempts) == 1 and attempts[0]["outcome"] == "completed" and not cleanup_pending:
                    assert attempts[0]["cleanup_error"] is None and attempts[0]["finished_at"] is not None
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Run attempt 未完成清理或发生了额外重跑")
            assert row["content"] == "PRIVATE_FILE_DENIED", dict(row)
            audits = await conn.fetch(
                "SELECT t.tool_name,t.tool_output FROM tool_calls t "
                "JOIN messages m ON m.id=t.message_id WHERE m.run_id=$1",
                run_id,
            )
            assert len(audits) == 1 and audits[0]["tool_name"] == "search_file"
            assert "无法获取当前会话可访问的知识库" in str(audits[0]["tool_output"])
            assert "revocation-sentinel.txt" not in str(audits)
            assert (
                await search_file.coroutine(query="revocation-sentinel", runtime=runtime)
                == "无法获取当前会话可访问的知识库"
            )
            # 共享恢复后原对象能再次读取，证明拒绝来自最新权限而非永久破坏工具。
            response = await api.put(
                f"/api/knowledge/databases/{kb_id}",
                headers=admin,
                json={"name": kb_id, "description": "合成恢复数据", "share_config": share},
            )
            assert response.status_code == 200
            assert "revocation-sentinel.txt" in str(
                await search_file.coroutine(query="revocation-sentinel", runtime=runtime)
            )
        finally:
            await model.post("/control/release", json={})
            await conn.close()
            await pg_manager.close()
