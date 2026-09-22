from io import StringIO
from unittest.mock import Mock

import pytest

from yuxi.storage.postgres import manager as manager_module


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_initialize_does_not_log_connection_secrets(monkeypatch, failure):
    """初始化成功和失败日志都不能包含连接串中的秘密。"""
    manager = object.__new__(manager_module.PostgresManager)
    manager.__init__()
    password = "synthetic-password-canary"
    query_secret = "synthetic-query-canary"
    url = f"postgresql+asyncpg://test:{password}@localhost/test?application_name={query_secret}"
    monkeypatch.setenv("POSTGRES_URL", url)
    monkeypatch.setattr(manager_module, "AsyncConnectionPool", Mock())
    if failure:

        def fail_engine(*args, **kwargs):
            """模拟驱动将连接串写入异常正文。"""
            raise ValueError(f"invalid connection: {url}")

        monkeypatch.setattr(manager_module, "create_async_engine", fail_engine)

    output = StringIO()
    sink = manager_module.logger.add(output, format="{message}")
    try:
        manager.initialize()
        logs = output.getvalue()
        assert manager._initialized is not failure
        assert logs
        assert password not in logs
        assert query_secret not in logs
        if failure:
            assert "ValueError" in logs
    finally:
        manager_module.logger.remove(sink)
        if manager.async_engine is not None:
            await manager.async_engine.dispose()
