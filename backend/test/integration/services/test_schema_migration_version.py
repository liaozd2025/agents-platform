"""Yuxi Schema 版本事实在真实 PostgreSQL 上的集成测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.storage.postgres.manager import BUSINESS_SCHEMA_VERSION, PostgresManager

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本文件自行创建隔离 Schema，不依赖运行中的 API。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """隔离 Schema 测试没有 HTTP 资源需要清理。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """隔离 Schema 测试没有 Sandbox 资源需要清理。"""
    yield


def _scoped_manager(engine) -> PostgresManager:
    """创建不触碰进程单例的隔离 manager。"""
    manager = object.__new__(PostgresManager)
    PostgresManager.__init__(manager)
    manager.async_engine = engine
    manager._initialized = True
    return manager


async def test_schema_migration_lock_serializes_real_postgres_sessions() -> None:
    """两个 migrator 竞争同一 advisory lock 时只允许一个进入临界区。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    manager = _scoped_manager(engine)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_migrator() -> None:
        async with manager.schema_migration_lock():
            first_entered.set()
            await release_first.wait()

    async def second_migrator() -> None:
        await first_entered.wait()
        async with manager.schema_migration_lock():
            second_entered.set()

    first_task = asyncio.create_task(first_migrator())
    second_task = asyncio.create_task(second_migrator())
    try:
        await asyncio.wait_for(first_entered.wait(), timeout=2)
        await asyncio.sleep(0.1)
        assert second_entered.is_set() is False
        release_first.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=2)
        assert second_entered.is_set() is True
    finally:
        release_first.set()
        for task in (first_task, second_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
        await engine.dispose()


async def test_schema_version_is_persisted_and_runtime_validation_fails_closed() -> None:
    """版本表缺失、错误和正确三种状态必须形成精确启动结论。"""
    schema = f"pytest_schema_version_{uuid.uuid4().hex[:16]}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    scoped_engine = None

    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        scoped_engine = create_async_engine(
            os.environ["POSTGRES_URL"],
            pool_pre_ping=True,
            connect_args={"server_settings": {"search_path": schema}},
        )
        manager = _scoped_manager(scoped_engine)

        with pytest.raises(RuntimeError, match="business=missing"):
            await manager.require_current_schema(include_knowledge=False)

        await manager.create_schema_version_table()
        await manager.record_schema_version("business", BUSINESS_SCHEMA_VERSION + 1)
        with pytest.raises(RuntimeError, match=f"business={BUSINESS_SCHEMA_VERSION + 1}"):
            await manager.require_current_schema(include_knowledge=False)

        await manager.record_schema_version("business", BUSINESS_SCHEMA_VERSION)
        await manager.require_current_schema(include_knowledge=False)
        assert await manager.get_schema_versions() == {"business": BUSINESS_SCHEMA_VERSION}
    finally:
        if scoped_engine is not None:
            await scoped_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.parametrize("starting_version", [1, 2, BUSINESS_SCHEMA_VERSION - 1])
async def test_supported_business_versions_run_real_ddl_before_advancing_version(starting_version: int) -> None:
    """真实 DDL 失败时保留原版本，成功补列后才允许记录当前版本。"""
    schema = f"pytest_business_upgrade_{uuid.uuid4().hex[:16]}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    scoped_engine = None

    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        scoped_engine = create_async_engine(
            os.environ["POSTGRES_URL"],
            pool_pre_ping=True,
            connect_args={"server_settings": {"search_path": schema}},
        )
        manager = _scoped_manager(scoped_engine)
        await manager.create_business_tables()
        await manager.create_knowledge_tables()
        await manager.create_schema_version_table()
        await manager.record_schema_version("business", starting_version)
        async with scoped_engine.begin() as connection:
            await connection.execute(text("ALTER TABLE tool_calls DROP COLUMN organization_path_snapshot"))
            await connection.execute(text("ALTER TABLE projects DROP COLUMN status CASCADE"))
            await connection.execute(text("ALTER TABLE projects DROP COLUMN deleted_at"))
            await connection.execute(
                text("INSERT INTO departments (id, name, node_type, path) VALUES (2, '缺根节点', 'department', '')")
            )

        async def column_exists(table_name: str, column_name: str) -> bool:
            async with scoped_engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = :table_name "
                        "AND column_name = :column_name)"
                    ),
                    {"table_name": table_name, "column_name": column_name},
                )
                return bool(result.scalar())

        with pytest.raises(Exception, match="id=1"):
            await manager.ensure_business_schema()
        assert await manager.get_schema_versions() == {"business": starting_version}
        assert await column_exists("tool_calls", "organization_path_snapshot") is False
        assert await column_exists("projects", "status") is False
        assert await column_exists("projects", "deleted_at") is False

        async with scoped_engine.begin() as connection:
            await connection.execute(text("DELETE FROM departments WHERE id = 2"))
        await manager.ensure_business_schema()
        assert await column_exists("tool_calls", "organization_path_snapshot") is True
        assert await column_exists("projects", "status") is True
        assert await column_exists("projects", "deleted_at") is True
        assert await manager.get_schema_versions() == {"business": starting_version}

        await manager.record_schema_version("business", BUSINESS_SCHEMA_VERSION)
        await manager.require_current_schema(include_knowledge=False)
    finally:
        if scoped_engine is not None:
            await scoped_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


async def test_pi_cleanup_lock_skips_concurrent_real_postgres_session() -> None:
    """同一 cleanup fact 同时只能由一个 worker 执行外部清理。"""
    schema = f"pytest_pi_cleanup_lock_{uuid.uuid4().hex[:16]}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    scoped_engine = None

    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped_engine = create_async_engine(
            os.environ["POSTGRES_URL"],
            pool_pre_ping=True,
            connect_args={"server_settings": {"search_path": schema}},
        )
        failed_at = datetime(2026, 8, 27, 12, 0, 0)
        async with scoped_engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE agent_run_attempts ("
                    "id INTEGER PRIMARY KEY, adapter VARCHAR(32), cleanup_failed_at TIMESTAMP)"
                )
            )
            await connection.execute(
                text("INSERT INTO agent_run_attempts (id, adapter, cleanup_failed_at) VALUES (7, 'local', :failed_at)"),
                {"failed_at": failed_at},
            )

        sessions = async_sessionmaker(scoped_engine, expire_on_commit=False)
        async with sessions() as first, sessions() as second:
            await first.begin()
            await second.begin()
            assert await AgentRunRepository(first).lock_pi_cleanup_failure(7, failed_at=failed_at) is True
            assert await AgentRunRepository(second).lock_pi_cleanup_failure(7, failed_at=failed_at) is False
            await first.commit()
            assert await AgentRunRepository(second).lock_pi_cleanup_failure(7, failed_at=failed_at) is True
            await second.rollback()
    finally:
        if scoped_engine is not None:
            await scoped_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
