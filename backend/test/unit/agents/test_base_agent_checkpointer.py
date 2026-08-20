import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from yuxi.agents.base import BaseAgent
from yuxi.agents.checkpointer_config import resolve_checkpointer_backend


@pytest.mark.asyncio
async def test_postgres_is_the_python_default_checkpointer(monkeypatch: pytest.MonkeyPatch):
    """非 Compose 启动也应默认使用 PostgreSQL；显式声明也走同一路径。"""
    agent = object.__new__(BaseAgent)
    agent.checkpointer = None
    saver = object()
    monkeypatch.delenv("LANGGRAPH_CHECKPOINTER_BACKEND", raising=False)
    monkeypatch.setattr("yuxi.agents.base.pg_manager", SimpleNamespace(get_langgraph_checkpointer=lambda: saver))

    assert await agent._get_checkpointer() is saver

    monkeypatch.setenv("LANGGRAPH_CHECKPOINTER_BACKEND", "postgres")
    assert await agent._get_checkpointer() is saver


@pytest.mark.asyncio
async def test_unknown_checkpointer_backend_is_rejected(monkeypatch: pytest.MonkeyPatch):
    """非法后端配置应立即暴露。"""
    agent = object.__new__(BaseAgent)
    agent.checkpointer = None
    monkeypatch.setenv("LANGGRAPH_CHECKPOINTER_BACKEND", "unknown")

    with pytest.raises(ValueError, match="不支持的 LangGraph checkpointer backend"):
        await agent._get_checkpointer()


def test_checkpointer_backend_owner_normalizes_supported_values_and_rejects_unknown() -> None:
    """API、worker 与 Agent 入口共享同一个配置解析结果。"""

    assert resolve_checkpointer_backend(" POSTGRES ") == "postgres"
    assert resolve_checkpointer_backend("SQLite") == "sqlite"
    with pytest.raises(ValueError, match="unsupported"):
        resolve_checkpointer_backend("unsupported")


@pytest.mark.asyncio
async def test_sqlite_initialization_failure_is_not_relabelled_as_memory(monkeypatch: pytest.MonkeyPatch):
    """显式 SQLite 失败时不能静默改变 checkpoint 的持久化语义。"""
    agent = object.__new__(BaseAgent)
    agent.checkpointer = None
    monkeypatch.setenv("LANGGRAPH_CHECKPOINTER_BACKEND", "sqlite")

    async def fail_to_connect():
        raise OSError("sqlite unavailable")

    monkeypatch.setattr(agent, "get_async_conn", fail_to_connect)

    with pytest.raises(OSError, match="sqlite unavailable"):
        await agent._get_checkpointer()


@pytest.mark.asyncio
async def test_sqlite_pragma_failure_closes_unpublished_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """PRAGMA 中途失败时关闭局部连接，后续调用创建全新连接。"""

    class FakeCursor:
        async def fetchall(self):
            return []

    class FakeConnection:
        def __init__(self, *, fail_on_execute: int | None = None):
            self.fail_on_execute = fail_on_execute
            self.execute_calls: list[str] = []
            self.closed = False

        async def execute(self, statement: str):
            self.execute_calls.append(statement)
            if len(self.execute_calls) == self.fail_on_execute:
                raise OSError("pragma initialization failed")
            return FakeCursor()

        async def close(self):
            self.closed = True

    failed_connection = FakeConnection(fail_on_execute=2)
    healthy_connection = FakeConnection()
    connections = iter((failed_connection, healthy_connection))

    async def connect(_path: str):
        return next(connections)

    monkeypatch.setattr("yuxi.agents.base.aiosqlite.connect", connect)
    agent = object.__new__(BaseAgent)
    agent._async_conn = None
    agent.workdir = str(tmp_path)

    with pytest.raises(OSError, match="pragma initialization failed"):
        await agent.get_async_conn()

    assert failed_connection.closed is True
    assert agent._async_conn is None

    assert await agent.get_async_conn() is healthy_connection
    assert agent._async_conn is healthy_connection


@pytest.mark.asyncio
async def test_sqlite_fetchall_cancellation_closes_unpublished_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """PRAGMA fetch 被取消时也要释放局部连接并保留取消语义。"""
    cursor = SimpleNamespace(fetchall=AsyncMock(side_effect=asyncio.CancelledError))
    connection = SimpleNamespace(
        execute=AsyncMock(return_value=cursor),
        close=AsyncMock(),
    )

    async def connect(_path: str):
        return connection

    monkeypatch.setattr("yuxi.agents.base.aiosqlite.connect", connect)
    agent = object.__new__(BaseAgent)
    agent._async_conn = None
    agent.workdir = str(tmp_path)

    with pytest.raises(asyncio.CancelledError):
        await agent.get_async_conn()

    connection.close.assert_awaited_once()
    assert agent._async_conn is None


@pytest.mark.asyncio
async def test_sqlite_compatibility_adapter_failure_closes_unpublished_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """无法安装 is_alive 兼容适配时不能留下未缓存连接。"""
    close_calls: list[str] = []

    class ConnectionWithoutDynamicAttributes:
        __slots__ = ()

        async def execute(self, _statement: str):
            return SimpleNamespace(fetchall=AsyncMock(return_value=[]))

        async def close(self):
            close_calls.append("close")

    connection = ConnectionWithoutDynamicAttributes()

    async def connect(_path: str):
        return connection

    monkeypatch.setattr("yuxi.agents.base.aiosqlite.connect", connect)
    agent = object.__new__(BaseAgent)
    agent._async_conn = None
    agent.workdir = str(tmp_path)

    with pytest.raises(AttributeError):
        await agent.get_async_conn()

    assert close_calls == ["close"]
    assert agent._async_conn is None


@pytest.mark.asyncio
async def test_sqlite_cleanup_failure_does_not_mask_initialization_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """清理自身失败时仍保留原始 PRAGMA 错误。"""
    connection = SimpleNamespace(
        execute=AsyncMock(side_effect=OSError("pragma initialization failed")),
        close=AsyncMock(side_effect=RuntimeError("close failed")),
    )

    async def connect(_path: str):
        return connection

    monkeypatch.setattr("yuxi.agents.base.aiosqlite.connect", connect)
    agent = object.__new__(BaseAgent)
    agent._async_conn = None
    agent.workdir = str(tmp_path)

    with pytest.raises(OSError, match="pragma initialization failed"):
        await agent.get_async_conn()

    connection.close.assert_awaited_once()
    assert agent._async_conn is None
