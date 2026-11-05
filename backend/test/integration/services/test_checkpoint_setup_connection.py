"""真实 PostgreSQL 上的 checkpoint 初始化只使用持锁物理连接。"""

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest
from psycopg import AsyncConnection

from yuxi.infrastructure.postgres.checkpointer import setup_langgraph_checkpointer

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


async def test_checkpoint_ddl_uses_locked_connection_and_releases_session_lock():
    """真实建表、迁移记录和锁释放共同验证新 app-init 的数据库边界。"""
    schema = "checkpoint_owner_" + uuid4().hex
    dsn = os.environ["POSTGRES_URL"].replace("+asyncpg", "")
    admin = await asyncpg.connect(dsn)
    connection = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        connection = await AsyncConnection.connect(dsn, autocommit=True, options=f"-c search_path={schema}")

        @asynccontextmanager
        async def held_connection():
            """只提供一个已知物理连接，不提供可另借连接的 pool。"""
            yield connection

        runtime_saver = object()
        manager = SimpleNamespace(
            langgraph_pool=SimpleNamespace(connection=held_connection),
            langgraph_checkpointer=runtime_saver,
            langgraph_checkpointer_setup=False,
        )
        assert await setup_langgraph_checkpointer(manager) is runtime_saver
        assert manager.langgraph_checkpointer_setup is True
        cursor = await connection.execute("SELECT count(*) FROM checkpoint_migrations")
        migration_count = (await cursor.fetchone())[0]
        assert migration_count > 0
        cursor = await connection.execute("SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'")
        assert (await cursor.fetchone())[0] == 0
        assert await setup_langgraph_checkpointer(manager) is runtime_saver
        cursor = await connection.execute("SELECT count(*) FROM checkpoint_migrations")
        assert (await cursor.fetchone())[0] == migration_count
    finally:
        if connection is not None:
            await connection.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()
