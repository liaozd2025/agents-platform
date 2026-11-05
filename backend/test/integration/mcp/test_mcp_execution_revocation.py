"""用真实 PostgreSQL 和 MCP 验证跨进程禁用后旧工具失效。"""

import asyncio
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from langchain.tools.tool_node import ToolCallRequest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test.integration.mcp.test_http_origin import mcp_server
from yuxi.infrastructure.postgres.base import Base
from yuxi.infrastructure.postgres.manager import pg_manager
from yuxi.modules.agents.runtime.agent_backends.chatbot.context import ChatBotContext
from yuxi.modules.agents.runtime.middlewares import authorization
from yuxi.modules.extensions.mcp import service
from yuxi.modules.extensions.mcp.models import MCPServer
from yuxi.modules.extensions.mcp.runtime import get_mcp_client
from yuxi.modules.identity.models import User

pytestmark = [pytest.mark.integration, pytest.mark.self_managed_resources]


@pytest_asyncio.fixture
async def mcp_database(monkeypatch):
    """只在显式测试数据库的新 schema 中建表，结束后清理。"""
    url = os.environ.get("TEST_MCP_POSTGRES_URL") or os.environ.get("POSTGRES_URL")
    if not url:
        pytest.fail("需要独立 PostgreSQL：请设置 TEST_MCP_POSTGRES_URL（postgresql+asyncpg://...）")
    monkeypatch.setenv("TEST_MCP_POSTGRES_URL", url)
    schema = f"mcp_test_{uuid.uuid4().hex}"
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(pg_manager, "_initialized", True)
        monkeypatch.setattr(pg_manager, "AsyncSession", sessions)
        async with sessions() as db:
            db.add(User(uid="fixture-user", username="Fixture", password_hash="!fixture"))
            await db.commit()

        async def actor(context, *, db):
            """只隔离本测试无关的 Agent/Run 外壳，MCP主体仍从当前数据库读取。"""
            user = await db.scalar(select(User).where(User.uid == context.uid, User.is_deleted == 0))
            assert user is not None
            return user, None, None

        async def retain_resources(*args, **kwargs):
            """保留已经准备好的资源选择；实际MCP可用策略不替换。"""

        monkeypatch.setattr(authorization, "_require_execution_actor", actor)
        monkeypatch.setattr(authorization, "refresh_agent_context_resources", retain_resources)
        yield sessions, schema
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


def tool_context(slug, tools):
    """装配当前运行已注册的工具集合，不重新发现或替换已持有对象。"""
    return ChatBotContext(
        uid="fixture-user",
        run_id=None,
        thread_id="fixture",
        mcps=[slug],
        tools=[],
        registered_builtin_tool_names=set(),
        middleware_tool_names=set(),
        mcp_tool_servers={tool.name: slug for tool in tools},
        mcp_unavailable={},
        skill_runtime_snapshot={
            "capability_limited": False,
            "context_skills": [],
            "context_preload_skills": [],
            "effective_skills": [],
            "runtime_skills": {},
            "skill_metadata": {},
            "preloaded_skills": [],
            "preloaded_skill_contents": {},
        },
    )


async def execute_held_tool(tool, context):
    """经生产执行拦截器验证当前策略，再进入SDK和真实HTTP传输。"""
    request = ToolCallRequest(
        tool=tool,
        tool_call={"name": tool.name, "args": {}, "id": "test-call", "type": "tool_call"},
        state={},
        runtime=SimpleNamespace(context=context),
    )

    async def invoke(request):
        """真实工具副作用只从已通过授权的handler发生。"""
        return await tool.ainvoke(request.tool_call)

    return await authorization.RuntimeAuthorizationMiddleware().awrap_tool_call(request, invoke)


async def change_in_other_process(schema, slug, action):
    """从另一进程提交产品管理操作，不触碰调用进程的工具缓存。"""
    script = """
import asyncio, os, sys
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from yuxi.modules.extensions.mcp import service

async def main():
    schema, slug, action = sys.argv[1:]
    engine = create_async_engine(os.environ['TEST_MCP_POSTGRES_URL'],
        connect_args={'server_settings': {'search_path': schema}})
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            if action == 'tool':
                await service.toggle_tool_enabled(db, slug, 'probe')
            elif action == 'delete':
                await service.delete_mcp_server(db, slug)
            else:
                await service.set_server_enabled(db, slug, action == 'enable')
    finally:
        await engine.dispose()
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        schema,
        slug,
        action,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        assert process.returncode == 0, (stdout + stderr).decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse", "streamable_http"])
@pytest.mark.parametrize("action", ["tool", "server", "delete"])
async def test_held_tool_rejects_committed_revocation(mcp_database, transport, action):
    """旧对象在另一进程禁用提交后拒绝，重新启用后可再次执行。"""
    sessions, schema = mcp_database
    with mcp_server(transport, extra_tool=True) as remote:
        async with sessions() as db:
            await service.create_mcp_server(
                db,
                slug="revocation",
                name="revocation",
                transport=transport,
                url=remote.url + remote.endpoint,
                created_by="test",
            )
        tools = await service.get_enabled_mcp_tools("revocation")
        assert len(tools) == 2
        held = next(tool for tool in tools if tool.name == "probe")
        other = next(tool for tool in tools if tool.name == "other_probe")
        context = tool_context("revocation", tools)
        result = await execute_held_tool(held, context)
        assert result.status == "success" and "MCP_ORIGIN_OK" in str(result.content)

        await change_in_other_process(schema, "revocation", action)
        remote.records.clear()
        result = await execute_held_tool(held, context)
        assert not remote.records, "禁用提交后，旧工具仍向 MCP 发出了请求"
        assert result.status == "error"
        assert "无权使用该工具" in str(result.content)
        available = await service.get_enabled_mcp_tools("revocation")
        assert [tool.name for tool in available] == (["other_probe"] if action == "tool" else [])
        if action == "tool":
            assert "MCP_ORIGIN_OK" in str(await execute_held_tool(other, context))

        if action != "delete":
            await change_in_other_process(schema, "revocation", "tool" if action == "tool" else "enable")
            result = await execute_held_tool(held, context)
            assert result.status == "success" and "MCP_ORIGIN_OK" in str(result.content)
            assert remote.records


@pytest.mark.asyncio
async def test_database_failure_cannot_execute_held_tool(mcp_database):
    """真实数据库查询失败时，已加载工具也不能连接远端。"""
    sessions, _ = mcp_database
    with mcp_server("streamable_http") as remote:
        async with sessions() as db:
            await service.create_mcp_server(
                db,
                slug="db-failure",
                name="db-failure",
                transport="streamable_http",
                url=remote.url + remote.endpoint,
                created_by="test",
            )
        tools = await service.get_enabled_mcp_tools("db-failure")
        held = tools[0]
        context = tool_context("db-failure", tools)
        assert "MCP_ORIGIN_OK" in str(await execute_held_tool(held, context))
        async with sessions() as db:
            await db.execute(text("ALTER TABLE mcp_servers RENAME TO unavailable_mcp_servers"))
            await db.commit()
        remote.records.clear()
        with pytest.raises(DBAPIError):
            await execute_held_tool(held, context)
        assert not remote.records


@pytest.mark.asyncio
async def test_retired_stdio_configuration_never_starts_process(mcp_database, tmp_path):
    """新远程协议不启动遗留stdio：启用记录和直接客户端调用均不能绕过边界。"""
    sessions, schema = mcp_database
    marker = tmp_path / "started.txt"
    script = tmp_path / "mcp_fixture.py"
    script.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('started')\n")
    slug = "retired-stdio"
    async with sessions() as db:
        db.add(MCPServer(slug=slug, name=slug, transport="stdio", enabled=1, created_by="test", updated_by="test"))
        await db.commit()
    assert await service.get_enabled_mcp_tools(slug) == []
    with pytest.raises(ValueError, match="MCP 仅支持"):
        await get_mcp_client({slug: {"transport": "stdio", "command": sys.executable, "args": [str(script)]}})
    assert not marker.exists()
    await change_in_other_process(schema, slug, "server")
    assert await service.get_enabled_mcp_tools(slug) == []
    assert not marker.exists()
