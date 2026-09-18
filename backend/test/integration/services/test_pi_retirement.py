"""真实 PostgreSQL 升级与 HTTP 历史兼容，不调用 PI 或外部模型。"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers.agent_router import agent_router
from server.routers.chat_router import chat
from server.utils.agent_permissions import require_agent_use_permission
from server.utils.auth_middleware import get_db
from yuxi import storage_migration
from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.storage_migrations.v073_pi_retirement import inspect_pi_retirement
from yuxi.repositories.user_repository import UserRepository
from yuxi.storage.postgres.manager import BUSINESS_SCHEMA_VERSION, KNOWLEDGE_SCHEMA_VERSION, PostgresManager
from yuxi.storage.postgres.models_business import (
    Agent,
    AgentRun,
    AgentRunAttempt,
    AgentRunRequest,
    Conversation,
    Message,
    Project,
)
from yuxi.utils.datetime_utils import utc_now_naive
from yuxi.workspace.paths import ensure_bound_user_workdir, user_workdir_host_dir

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本文件使用自己的 PostgreSQL Schema 与 ASGI 应用。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """不访问现有 API 的资源。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """本文件不创建 Sandbox。"""
    yield


class _Model(GenericFakeChatModel):
    def bind_tools(self, _tools, **_kwargs):
        """用固定模型输出验证真实 LangGraph checkpoint 续接。"""
        return self


@tool
def pi_sandbox(description: str) -> str:
    """已退役工具；任何测试执行到此都说明发生了旧任务重跑。"""
    raise AssertionError("旧 PI 工具不可执行")


@tool
def execute(command: str) -> str:
    """普通工具，仅用于构造需要审批的历史 checkpoint。"""
    return command


@pytest_asyncio.fixture
async def database(tmp_path, monkeypatch):
    """建立只属于本测试的数据库、checkpoint 和 Workdir。"""
    schema = f"pytest_pi_retirement_{uuid.uuid4().hex[:12]}"
    url = os.environ["POSTGRES_URL"]
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    for name in (
        "YUXI_USER_DATA_DIR",
        "YUXI_SKILL_DATA_DIR",
        "YUXI_SKILL_PROJECTION_DIR",
        "YUXI_LEGACY_STORAGE_DIR",
        "YUXI_RUNTIME_DIR",
    ):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setattr(storage_migration, "runtime_storage_requires_quiescence", lambda: False)
    monkeypatch.setattr(storage_migration, "migrate_runtime_storage_identity", lambda: None)
    managers = []

    def make_manager():
        """独立连接池保持 Schema 隔离，迁移关闭连接后可以重建。"""
        manager = object.__new__(PostgresManager)
        PostgresManager.__init__(manager)
        manager.async_engine = engine
        manager.AsyncSession = async_sessionmaker(engine, expire_on_commit=False)
        manager.langgraph_pool = AsyncConnectionPool(
            url.replace("postgresql+asyncpg://", "postgresql://"),
            open=False,
            kwargs={"autocommit": True, "options": f"-csearch_path={schema}"},
        )
        manager._initialized = True
        managers.append(manager)
        return manager

    try:
        manager = make_manager()
        await manager.langgraph_pool.open()
        await manager.create_business_tables()
        await manager.ensure_business_schema()
        await manager.create_knowledge_tables()
        await manager.ensure_knowledge_schema()
        await manager.create_schema_version_table()
        await manager.record_schema_version("business", 8)
        await manager.record_schema_version("knowledge", KNOWLEDGE_SCHEMA_VERSION)
        await manager.setup_langgraph_checkpointer()
        async with engine.begin() as conn:
            # 旧版允许活动 sandbox；新约束必须在退役后禁止它，历史终态仍可保留。
            await conn.execute(text("ALTER TABLE agent_runs DROP CONSTRAINT ck_agent_runs_nonterminal_shape"))
        project_id = str(uuid.uuid4())
        async with manager.get_async_session_context() as db:
            user = await UserRepository(db).create_with_db(
                db,
                {"uid": "owner", "username": "owner", "password_hash": "unused-test-hash"},
                default_role_code="superadmin",
            )
            db.add(
                Agent(
                    slug="main",
                    backend_id="ChatbotAgent",
                    name="测试",
                    created_by=user.uid,
                    share_config={"version": 2, "read_scope": {"access_level": "global"}, "manage_scope": None},
                )
            )
            db.add(
                Project(
                    id=project_id,
                    uid=user.uid,
                    selection_status="implicit",
                    directory_mode="managed",
                    workdir_path=f"projects/{project_id}",
                )
            )
            await db.flush()
            configs = {
                "history": ("completed", "chat", None, {}),
                "legacy": (
                    "completed",
                    "sandbox",
                    "history",
                    {"runtime": {"executor": "pi", "tool_call_id": "pi-old"}},
                ),
                "active-parent": ("running", "chat", None, {}),
                "active-child": ("running", "sandbox", "active-parent", {"runtime": {"executor": "pi"}}),
                "pi-approval": ("interrupted", "chat", None, {}),
                "ordinary-approval": ("interrupted", "chat", None, {}),
                "ordinary-pending": ("pending", "chat", None, {}),
                "pi-pending": ("pending", "chat", None, {"runtime": {"executor": "pi"}}),
            }
            for run_id, (status, run_type, parent, payload) in configs.items():
                conversation = Conversation(
                    thread_id=run_id,
                    uid=user.uid,
                    project_id=project_id,
                    agent_id="main",
                    status="subagent" if run_type == "sandbox" else "active",
                    extra_metadata={"source": "pi_sandbox"} if run_type == "sandbox" else {},
                )
                db.add(conversation)
                await db.flush()
                message = Message(
                    conversation_id=conversation.id,
                    role="user",
                    content="保留输入",
                    request_id=f"req-{run_id}",
                    delivery_status="dispatched",
                )
                db.add(message)
                await db.flush()
                run = AgentRun(
                    id=run_id,
                    uid=user.uid,
                    agent_slug="main",
                    conversation_id=conversation.id,
                    conversation_thread_id=run_id,
                    runtime_scope_id=parent or run_id,
                    run_type=run_type,
                    created_by_run_id=parent,
                    request_id=f"req-{run_id}",
                    status=status,
                    input_message_id=message.id,
                    input_payload={"model_spec": "test:model", **payload},
                    worker_id="old-worker" if status == "running" else None,
                    lease_expires_at=utc_now_naive() + timedelta(minutes=5) if status == "running" else None,
                )
                db.add(run)
                await db.flush()
                message.run_id = run_id
                if run_id == "legacy":
                    metadata = {"pi": {"artifact": {"files": [{"path": "report.txt"}]}, "output_subdir": "pi-runs/old"}}
                    output = Message(
                        conversation_id=conversation.id,
                        role="assistant",
                        content="历史 PI 结果",
                        run_id=run.id,
                        extra_metadata=metadata,
                    )
                    db.add(output)
                    await db.flush()
                    run.output_message_id = output.id
                    db.add(
                        AgentRunAttempt(
                            run_id=run.id,
                            attempt_no=1,
                            worker_id="old",
                            adapter="local",
                            started_at=utc_now_naive(),
                            finished_at=utc_now_naive(),
                            result_events=[{"type": "final"}],
                            final_acked_at=utc_now_naive(),
                        )
                    )
                if run_id == "pi-pending":
                    db.add(
                        AgentRunRequest(
                            request_id="queued-pi",
                            uid=user.uid,
                            agent_slug="main",
                            conversation_thread_id=run_id,
                            input_message_id=message.id,
                            input_payload=payload,
                            status="queued",
                        )
                    )
        ensure_bound_user_workdir(user.uid, f"projects/{project_id}")
        artifact = user_workdir_host_dir(user.uid, f"projects/{project_id}") / "outputs/pi-runs/old/report.txt"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("历史文件字节", encoding="utf-8")
        for thread_id, tool_name in [("pi-approval", "pi_sandbox"), ("ordinary-approval", "execute")]:
            model = _Model(
                messages=iter(
                    [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": tool_name,
                                    "args": {"description" if tool_name == "pi_sandbox" else "command": "旧任务"},
                                    "id": f"call-{thread_id}",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    ]
                )
            )
            graph = create_agent(
                model,
                tools=[pi_sandbox, execute],
                middleware=[PatchToolCallsMiddleware(), HumanInTheLoopMiddleware(interrupt_on={tool_name: True})],
                checkpointer=manager.get_langgraph_checkpointer(),
            )
            history = []
            if tool_name == "execute":
                # 普通审批允许保留已完成的 PI 历史，不能按历史出现过 PI 就关闭新任务。
                history = [
                    AIMessage(
                        content="",
                        tool_calls=[{"name": "pi_sandbox", "args": {"description": "已完成"}, "id": "old-pi"}],
                    ),
                    ToolMessage(content="历史 PI 结果", tool_call_id="old-pi"),
                ]
            output = await graph.ainvoke(
                {"messages": [*history, HumanMessage("旧请求")]}, {"configurable": {"thread_id": thread_id}}
            )
            assert output["__interrupt__"]
        yield manager, make_manager, user, project_id, artifact
    finally:
        for item in managers:
            await item.close()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def test_upgrade_requires_shutdown_then_retires_pi_without_losing_history(database, tmp_path, monkeypatch):
    """拒绝热升级；停止后仅 PI 执行/等待被关闭，普通审批与产物不变。"""
    manager, make_manager, _user, _project_id, artifact = database
    monkeypatch.setattr(storage_migration, "pg_manager", manager)
    monkeypatch.setenv("YUXI_STORAGE_MIGRATION_QUIESCENCE_FILE", str(tmp_path / "proof"))
    monkeypatch.delenv("YUXI_STORAGE_MIGRATION_QUIESCENCE_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="migrate-storage.sh"):
        await storage_migration.main()
    for attempt in range(2):
        manager = make_manager()
        await manager.langgraph_pool.open()
        assert (await manager.get_schema_versions())["business"] == (8 if attempt == 0 else BUSINESS_SCHEMA_VERSION)
        (tmp_path / "proof").write_text("isolated-stop-proof")
        monkeypatch.setenv("YUXI_STORAGE_MIGRATION_QUIESCENCE_TOKEN", "isolated-stop-proof")
        monkeypatch.setattr(storage_migration, "pg_manager", manager)
        await storage_migration.main()
        manager = make_manager()
        await manager.langgraph_pool.open()
        assert (await manager.get_schema_versions())["business"] == BUSINESS_SCHEMA_VERSION
        async with manager.get_async_session_context() as db:
            runs = {run.id: run for run in (await db.execute(select(AgentRun))).scalars()}
            for run_id in ["active-parent", "active-child", "pi-pending", "pi-approval"]:
                assert (runs[run_id].status, runs[run_id].error_type) == ("failed", "pi_executor_retired")
                assert runs[run_id].worker_id is None and not runs[run_id].runtime_cleanup_pending
            assert runs["ordinary-approval"].status == "interrupted"
            assert runs["ordinary-pending"].status == "pending"
            assert runs["legacy"].status == "completed"
            assert (
                await db.scalar(select(AgentRunRequest.status).where(AgentRunRequest.request_id == "queued-pi"))
            ) == "failed"
            assert (
                await db.scalar(select(AgentRunAttempt.result_events).where(AgentRunAttempt.run_id == "legacy"))
            ) == [{"type": "final"}]
            if attempt == 1:
                _, acquired = await AgentRunRepository(db).mark_running(
                    "ordinary-pending", worker_id="new", lease_seconds=30
                )
                assert acquired
                new_attempt = await db.scalar(
                    select(AgentRunAttempt).where(AgentRunAttempt.run_id == "ordinary-pending")
                )
                assert new_attempt.adapter is None and new_attempt.result_events == []
        assert artifact.read_text() == "历史文件字节"
    # 同一真实 PostgreSQL checkpoint 保留旧记录，新消息只走新工具图。
    graph = create_agent(
        _Model(messages=iter([AIMessage(content="新请求已完成")])),
        tools=[execute],
        middleware=[PatchToolCallsMiddleware()],
        checkpointer=manager.get_langgraph_checkpointer(),
    )
    output = await graph.ainvoke({"messages": [HumanMessage("新请求")]}, {"configurable": {"thread_id": "pi-approval"}})
    assert output["messages"][-1].content == "新请求已完成"
    assert any(message.type == "tool" and "cancelled" in message.content for message in output["messages"])


async def test_history_http_remains_read_only_with_unchanged_download(database):
    """真实路由及数据库证明旧 PI 可读可下载，但新建、续跑和跨用户读取均被拒绝。"""
    manager, _make_manager, user, project_id, _artifact = database
    app = FastAPI()
    app.include_router(agent_router, prefix="/api")
    app.include_router(chat, prefix="/api")

    async def db_dependency():
        """每次 HTTP 请求使用独立真实事务。"""
        async with manager.get_async_session_context() as db:
            yield db

    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[require_agent_use_permission] = lambda: user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get("/api/agent/runs/legacy/result")
        assert result.status_code == 200 and result.json()["output"] == "历史 PI 结果"
        assert result.json()["pi"]["artifact"]["files"] == [{"path": "report.txt"}]
        state = await client.get("/api/chat/thread/legacy/state?include_messages=true")
        assert state.status_code == 200, state.text
        assert state.json()["messages"][0]["content"] == "历史 PI 结果"
        path = f"home/gem/user-data/projects/{project_id}/outputs/pi-runs/old/report.txt"
        download = await client.get(f"/api/chat/thread/legacy/artifacts/{path}?download=true")
        assert download.status_code == 200 and download.content == "历史文件字节".encode()
        for addition in [
            {"query": "继续"},
            {"resume": {"decisions": [{"type": "approve"}]}, "created_by_run_id": "legacy"},
        ]:
            denied = await client.post(
                "/api/agent/runs", json={"agent_slug": "main", "thread_id": "legacy", **addition}
            )
            assert denied.status_code == 409 and "仅可查看和下载" in denied.text
        denied = await client.post(
            "/api/agent/runs", json={"agent_slug": "main", "thread_id": "history", "query": "新PI", "executor": "pi"}
        )
        assert denied.status_code == 422
        user.uid = "other-owner"
        hidden = await client.get("/api/chat/thread/legacy/state?include_messages=true")
        assert hidden.status_code == 404
        hidden_file = await client.get(f"/api/chat/thread/legacy/artifacts/{path}?download=true")
        assert hidden_file.status_code == 404


@pytest.mark.parametrize("parent_column", ["parent_run_id", "parent_agent_run_id"])
async def test_upgrade_inspects_legacy_run_columns_before_schema_ddl(database, tmp_path, monkeypatch, parent_column):
    """旧列形状先检查 PI 停机证明，再正常升级和退役，拒绝时不修改旧表。"""
    manager, make_manager, _user, _project_id, _artifact = database
    async with manager.async_engine.begin() as connection:
        await connection.execute(text("ALTER TABLE agent_runs RENAME COLUMN conversation_thread_id TO thread_id"))
        await connection.execute(text(f"ALTER TABLE agent_runs RENAME COLUMN created_by_run_id TO {parent_column}"))
    await manager.record_schema_version("business", 2)
    monkeypatch.setattr(storage_migration, "pg_manager", manager)
    monkeypatch.setenv("YUXI_STORAGE_MIGRATION_QUIESCENCE_FILE", str(tmp_path / "proof"))
    monkeypatch.delenv("YUXI_STORAGE_MIGRATION_QUIESCENCE_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="migrate-storage.sh"):
        await storage_migration.main()

    manager = make_manager()
    await manager.langgraph_pool.open()
    async with manager.get_async_session_context() as db:
        columns = set(
            (
                await db.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = 'agent_runs'"
                    )
                )
            ).scalars()
        )
        assert "thread_id" in columns and "conversation_thread_id" not in columns
        assert (await db.scalar(text("SELECT status FROM agent_runs WHERE id = 'active-child'"))) == "running"
    assert (await manager.get_schema_versions())["business"] == 2
    (tmp_path / "proof").write_text("isolated-stop-proof")
    monkeypatch.setenv("YUXI_STORAGE_MIGRATION_QUIESCENCE_TOKEN", "isolated-stop-proof")
    monkeypatch.setattr(storage_migration, "pg_manager", manager)
    await storage_migration.main()
    manager = make_manager()
    await manager.langgraph_pool.open()
    assert (await manager.get_schema_versions())["business"] == BUSINESS_SCHEMA_VERSION
    async with manager.get_async_session_context() as db:
        for run_id in ("active-parent", "active-child", "pi-approval"):
            run = await db.get(AgentRun, run_id)
            assert (run.status, run.error_type) == ("failed", "pi_executor_retired")
        assert (await db.get(AgentRun, "active-child")).created_by_run_id == "active-parent"
        assert (await db.get(AgentRun, "ordinary-approval")).status == "interrupted"


@pytest.mark.parametrize("resume_status,approved_checkpoint", [("pending", False), ("running", True)])
async def test_upgrade_retires_approved_pi_resume_without_touching_ordinary_resume(
    database, tmp_path, monkeypatch, resume_status, approved_checkpoint
):
    """PI 审批已回复也不得恢复；同形状的普通审批和续跑仍保留。"""
    manager, make_manager, _user, _project_id, _artifact = database
    for thread_id, tool_name in (("pi-approval", "pi_sandbox"), ("ordinary-approval", "execute")):
        if approved_checkpoint:
            graph = create_agent(
                _Model(messages=iter([AIMessage(content="不应运行模型")])),
                tools=[pi_sandbox, execute],
                middleware=[PatchToolCallsMiddleware(), HumanInTheLoopMiddleware(interrupt_on={tool_name: True})],
                checkpointer=manager.get_langgraph_checkpointer(),
            )
            config = {"configurable": {"thread_id": thread_id}}
            await graph.ainvoke(
                Command(resume={"decisions": [{"type": "approve"}]}), config, interrupt_before=["tools"]
            )
            state = await graph.aget_state(config)
            assert state.next == ("tools",)
            assert not any(task.interrupts for task in state.tasks)
        async with manager.get_async_session_context() as db:
            parent = await db.get(AgentRun, thread_id)
            db.add(
                AgentRun(
                    id=f"{thread_id}-resume",
                    uid=parent.uid,
                    agent_slug=parent.agent_slug,
                    conversation_id=parent.conversation_id,
                    conversation_thread_id=thread_id,
                    runtime_scope_id=thread_id,
                    run_type="resume",
                    created_by_run_id=parent.id,
                    request_id=f"req-{thread_id}-resume",
                    input_message_id=parent.input_message_id,
                    input_payload={"model_spec": "test:model"},
                    status=resume_status,
                    worker_id="old-worker" if resume_status == "running" else None,
                    lease_expires_at=utc_now_naive() + timedelta(minutes=5) if resume_status == "running" else None,
                    created_at=parent.created_at + timedelta(seconds=1),
                )
            )
    run_ids, _request_ids, needs_proof = await inspect_pi_retirement(manager)
    assert {"pi-approval", "pi-approval-resume"} <= run_ids
    assert {"ordinary-approval", "ordinary-approval-resume"}.isdisjoint(run_ids)
    assert needs_proof
    (tmp_path / "proof").write_text("isolated-stop-proof")
    monkeypatch.setenv("YUXI_STORAGE_MIGRATION_QUIESCENCE_FILE", str(tmp_path / "proof"))
    monkeypatch.setenv("YUXI_STORAGE_MIGRATION_QUIESCENCE_TOKEN", "isolated-stop-proof")
    monkeypatch.setattr(storage_migration, "pg_manager", manager)
    await storage_migration.main()
    manager = make_manager()
    await manager.langgraph_pool.open()
    async with manager.get_async_session_context() as db:
        for run_id in ("pi-approval", "pi-approval-resume"):
            run = await db.get(AgentRun, run_id)
            assert (run.status, run.error_type) == ("failed", "pi_executor_retired")
        assert (await db.get(AgentRun, "ordinary-approval")).status == "interrupted"
        assert (await db.get(AgentRun, "ordinary-approval-resume")).status == resume_status
