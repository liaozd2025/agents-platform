"""Web 历史分页不暴露自动任务，任务详情与来源保持可访问。"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select

from test.integration.support.module_audit import isolated_audit_store, serve_http
from yuxi.api.errors import domain_error_handler
from yuxi.api.routers.agents import session_links
from yuxi.modules.agents.models.runs import AgentRun
from yuxi.modules.agents.models.sessions import Session
from yuxi.modules.agents.models.turns import AgentTurn
from yuxi.modules.agents.scope import ActorScope
from yuxi.modules.agents.services.threads import list_session_page
from yuxi.modules.identity.models import User
from yuxi.modules.workspace.models import Project
from yuxi.shared.datetime import utc_now
from yuxi.shared.errors import DomainError

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


async def test_web_history_pages_user_sessions_and_preserves_generated_navigation(monkeypatch):
    """过滤发生在分页前；置顶、身份隔离及原任务导航不受影响。"""
    async with isolated_audit_store(monkeypatch) as (_, sessions):
        now = utc_now()
        async with sessions() as db:
            db.add_all([User(uid=uid, username=uid, password_hash="fixture") for uid in ["history-user", "other-user"]])
            await db.flush()
            db.add_all(
                [
                    Project(id=uid, uid=uid, selection_status="implicit", directory_mode="managed", workdir_path=f"projects/{uid}")
                    for uid in ["history-user", "other-user"]
                ]
            )
            await db.flush()

            def session(tid, age, **kwargs):
                """明确构造有序数据，避免共享被测过滤逻辑。"""
                uid = kwargs.pop("uid", "history-user")
                return Session(
                    thread_id=tid,
                    tree_root_thread_id=kwargs.pop("tree_root_thread_id", tid),
                    uid=uid,
                    project_id=uid,
                    agent_id="fixture",
                    title=tid,
                    config_snapshot={"model": "fixture:model", "tool_approval_mode": "default"},
                    created_at=now - timedelta(seconds=age),
                    **kwargs,
                )

            root = session("user-old", 20, is_pinned=True)
            db.add_all(
                [
                    root,
                    session("user-new", 10),
                    session("other", 0, uid="other-user"),
                    session("app-only", 0, app_id="other-app"),
                    session("archived", 0, status="archived"),
                ]
            )
            await db.flush()
            db.add(AgentTurn(id="source-turn", thread_id=root.thread_id, uid=root.uid, status="completed"))
            await db.flush()
            db.add(
                AgentRun(
                    id="source-run",
                    turn_id="source-turn",
                    thread_id=root.thread_id,
                    runtime_scope_id=root.thread_id,
                    session_record_id=root.id,
                    uid=root.uid,
                    agent_slug="fixture",
                    status="completed",
                )
            )
            await db.flush()
            db.add_all(
                [
                    session("generated-root", 1, created_by_run_id="source-run", is_pinned=True),
                    session(
                        "generated-child",
                        2,
                        parent_thread_id=root.thread_id,
                        tree_root_thread_id=root.thread_id,
                        cooperation_path="/root/member",
                        cooperation_name="member",
                        created_by_run_id="source-run",
                    ),
                ]
            )
            await db.commit()
        app = FastAPI()
        app.add_exception_handler(DomainError, domain_error_handler)
        app.include_router(session_links.router, prefix="/api")
        app.dependency_overrides[session_links.get_required_user] = lambda: SimpleNamespace(uid="history-user")

        async def get_db():
            """请求使用独立真实事务。"""
            async with sessions() as db:
                yield db

        app.dependency_overrides[session_links.get_db] = get_db

        @app.middleware("http")
        async def simulate_api_key(request: Request, call_next):
            """替代认证边界的已认证 Key 标记。"""
            if request.headers.get("x-test-api-key"):
                request.state.api_key = object()
            return await call_next(request)

        async with serve_http(app) as client:
            first = await client.get("/api/agent/threads/history", params={"limit": 1})
            assert first.status_code == 200, first.text
            assert [item["id"] for item in first.json()["data"]] == ["user-new"]
            assert first.json()["has_more"] is True
            second = await client.get("/api/agent/threads/history", params={"limit": 1, "after": first.json()["last_id"]})
            assert [item["id"] for item in second.json()["data"]] == ["user-old"]
            assert second.json()["has_more"] is False
            pinned = await client.get("/api/agent/threads/history", params={"is_pinned": True})
            assert [item["id"] for item in pinned.json()["data"]] == ["user-old"]
            assert (await client.get("/api/agent/threads/history", headers={"x-test-api-key": "fixture"})).status_code == 403
            for tid in ["generated-root", "generated-child"]:
                navigation = await client.get(f"/api/agent/threads/{tid}/navigation")
                assert navigation.status_code == 200
                assert navigation.json()["session_id"] == tid
            assert (await client.get("/api/agent/threads/other/navigation")).status_code == 404
        async with sessions() as db:
            complete, _ = await list_session_page(
                db=db, scope=ActorScope(uid="history-user", app_id=None), agent_slug=None, after=None, limit=100, order="desc"
            )
            assert {item["id"] for item in complete} == {"user-new", "user-old", "generated-root", "generated-child"}
            saved = await db.scalar(select(Session).where(Session.thread_id == "generated-root"))
            assert saved.created_by_run_id == "source-run" and saved.parent_thread_id is None
