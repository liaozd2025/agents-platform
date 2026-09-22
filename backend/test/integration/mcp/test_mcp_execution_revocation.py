"""用真实 PostgreSQL 和 MCP 验证跨进程禁用后旧工具失效。"""

import asyncio
import os
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test.integration.mcp.test_http_origin import mcp_server
from yuxi.agents.mcp import service
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import MCPServer


@pytest_asyncio.fixture
async def mcp_database(monkeypatch):
    """只在显式测试数据库的新 schema 中建表，结束后清理。"""
    url = os.environ.get("TEST_MCP_POSTGRES_URL")
    if not url:
        pytest.fail("需要独立 PostgreSQL：请设置 TEST_MCP_POSTGRES_URL（postgresql+asyncpg://...）")
    schema = f"mcp_test_{uuid.uuid4().hex}"
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(MCPServer.__table__.create)
        monkeypatch.setattr(pg_manager, "_initialized", True)
        monkeypatch.setattr(pg_manager, "AsyncSession", sessions)
        service.clear_mcp_cache()
        yield sessions, schema
    finally:
        service.clear_mcp_cache()
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def change_in_other_process(schema, slug, action):
    """从另一进程提交产品管理操作，不触碰调用进程的工具缓存。"""
    script = """
import asyncio, os, sys
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from yuxi.agents.mcp import service

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
        call = {"name": "probe", "args": {}, "id": "test-call", "type": "tool_call"}
        result = await held.ainvoke(call)
        assert result.status == "success" and "MCP_ORIGIN_OK" in str(result.content)

        await change_in_other_process(schema, "revocation", action)
        remote.records.clear()
        result = await held.ainvoke(call)
        assert not remote.records, "禁用提交后，旧工具仍向 MCP 发出了请求"
        assert result.status == "error"
        assert "已禁用或删除" in str(result.content)
        available = await service.get_enabled_mcp_tools("revocation")
        assert [tool.name for tool in available] == (["other_probe"] if action == "tool" else [])
        if action == "tool":
            assert "MCP_ORIGIN_OK" in str(await other.ainvoke({}))

        if action != "delete":
            await change_in_other_process(schema, "revocation", "tool" if action == "tool" else "enable")
            result = await held.ainvoke(call)
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
        held = (await service.get_enabled_mcp_tools("db-failure"))[0]
        assert "MCP_ORIGIN_OK" in str(await held.ainvoke({}))
        async with sessions() as db:
            await db.execute(text("ALTER TABLE mcp_servers RENAME TO unavailable_mcp_servers"))
            await db.commit()
        remote.records.clear()
        with pytest.raises(DBAPIError):
            await held.ainvoke({})
        assert not remote.records


@pytest.mark.asyncio
async def test_builtin_stdio_revocation_prevents_process_start(mcp_database, monkeypatch, tmp_path):
    """内置 stdio 被禁用后旧工具不能再启动服务进程。"""
    sessions, schema = mcp_database
    marker = tmp_path / "started.txt"
    script = tmp_path / "mcp_fixture.py"
    script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "from mcp.server.fastmcp import FastMCP\n"
        "with Path(sys.argv[1]).open('a') as f: f.write('started\\n')\n"
        "mcp = FastMCP('fixture')\n"
        "@mcp.tool()\n"
        "def probe() -> str:\n"
        "    return 'STDIO_OK'\n"
        "mcp.run()\n"
    )
    slug = "mcp-server-chart"
    monkeypatch.setitem(
        service._DEFAULT_MCP_SERVERS,
        slug,
        {"transport": "stdio", "command": sys.executable, "args": [str(script), str(marker)]},
    )
    async with sessions() as db:
        db.add(MCPServer(slug=slug, name=slug, transport="stdio", enabled=1, created_by="test", updated_by="test"))
        await db.commit()
    held = (await service.get_enabled_mcp_tools(slug))[0]
    assert "STDIO_OK" in str(await held.ainvoke({}))
    started = marker.read_text()
    assert started == "started\nstarted\n"
    await change_in_other_process(schema, slug, "server")
    result = await held.ainvoke({"name": "probe", "args": {}, "id": "stdio-call", "type": "tool_call"})
    assert result.status == "error"
    assert marker.read_text() == started
