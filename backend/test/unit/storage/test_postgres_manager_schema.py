from __future__ import annotations

import pytest
from yuxi.storage.postgres.manager import PostgresManager


class _RecordingConnection:
    def __init__(self):
        self.statements: list[str] = []

    async def execute(self, statement, params=None):
        self.statements.append(str(statement))


class _RecordingBegin:
    def __init__(self, connection: _RecordingConnection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingEngine:
    def __init__(self, connection: _RecordingConnection):
        self.connection = connection

    def begin(self):
        return _RecordingBegin(self.connection)


@pytest.mark.asyncio
async def test_ensure_business_schema_backfills_subagent_thread_columns_before_dropping_legacy_columns():
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)

    assert "SET agent_slug = agent_id" in statements
    assert "SET conversation_thread_id = thread_id" in statements
    assert "SET created_by_run_id = COALESCE(parent_agent_run_id, parent_run_id)" in statements
    assert "SET subagent_slug = c.agent_id" in statements
    assert "SET created_by_run_id = created_by_parent_run_id::VARCHAR" in statements
    assert "ALTER COLUMN subagent_slug SET NOT NULL" in statements
    assert "ALTER COLUMN created_by_run_id SET NOT NULL" in statements
    assert statements.index("SET agent_slug = agent_id") < statements.index("DROP COLUMN IF EXISTS agent_id")
    assert statements.index("SET conversation_thread_id = thread_id") < statements.index(
        "DROP COLUMN IF EXISTS thread_id"
    )
    assert statements.index("COALESCE(parent_agent_run_id, parent_run_id)") < statements.index(
        "DROP COLUMN IF EXISTS parent_agent_run_id"
    )
    assert statements.index("created_by_parent_run_id") < statements.index(
        "DROP COLUMN IF EXISTS created_by_parent_run_id"
    )


@pytest.mark.asyncio
async def test_ensure_business_schema_cleans_duplicate_active_agent_runs_before_unique_index():
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)

    assert "WITH duplicated_active_runs AS" in statements
    assert "active_run_migration_conflict" in statements
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_one_active_per_thread" in statements
    assert statements.index("WITH duplicated_active_runs AS") < statements.index(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_one_active_per_thread"
    )


@pytest.mark.asyncio
async def test_ensure_business_schema_backfills_unviewed_marker_for_no_run_threads():
    """没有 chat/resume Run 的历史会话要写入未读哨兵，确保回填探测条件收敛为 false。"""
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)

    assert "SELECT EXISTS (SELECT 1 FROM conversations WHERE last_viewed_run_id IS NULL)" in statements
    assert "SET last_viewed_run_id = r.run_id" in statements
    assert "SET last_viewed_run_id = :marker WHERE last_viewed_run_id IS NULL" in statements
    assert statements.index("SET last_viewed_run_id = r.run_id") < statements.index(
        "SET last_viewed_run_id = :marker WHERE last_viewed_run_id IS NULL"
    )


@pytest.mark.asyncio
async def test_ensure_business_schema_creates_user_config_table():
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)

    assert "CREATE TABLE IF NOT EXISTS user_config" in statements
    assert "enable_memory BOOLEAN NOT NULL DEFAULT FALSE" in statements


@pytest.mark.asyncio
async def test_ensure_business_schema_creates_generic_config_options_table():
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)

    assert "CREATE TABLE IF NOT EXISTS config_options" in statements
    assert "params JSONB NOT NULL" in statements
    assert "value JSONB NOT NULL" in statements
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ix_config_options_key" in statements


@pytest.mark.asyncio
async def test_ensure_business_schema_backfills_builtin_role_assignments_idempotently():
    """角色扩展迁移应保留旧字段，并可重复回填同一用户角色。"""
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)

    for table_name in (
        "roles",
        "role_permissions",
        "role_default_departments",
        "user_role_assignments",
        "user_role_assignment_departments",
        "security_audits",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table_name}" in statements

    assert "INSERT INTO roles" in statements
    assert "'superadmin'" in statements
    assert "'admin'" in statements
    assert "'user'" in statements
    assert ("('superadmin', '超级管理员', '拥有全部功能权限和全部数据范围', TRUE, TRUE, 'all')") in statements
    assert "INSERT INTO user_role_assignments" in statements
    assert "WHERE NOT EXISTS" in statements
    assert "ON CONFLICT (user_id, role_id) DO NOTHING" in statements
    assert "ALTER TABLE security_audits ADD COLUMN IF NOT EXISTS reason TEXT" in statements
    assert "DELETE FROM role_permissions WHERE permission_key = 'agent:use'" in statements
    assert "DROP COLUMN IF EXISTS role" not in statements


@pytest.mark.asyncio
async def test_ensure_business_schema_fails_when_nonempty_organization_has_no_group_root():
    """存量组织表缺少固定集团根时应中止迁移，避免继续运行无效树。"""
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)
    assert "RAISE EXCEPTION" in statements
    assert "departments 非空但缺少 id=1 的集团根" in statements


@pytest.mark.asyncio
async def test_ensure_business_schema_adds_run_origin_snapshot_columns():
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)
    assert "agent_runs ADD COLUMN IF NOT EXISTS source VARCHAR(32)" in statements
    assert "agent_runs ADD COLUMN IF NOT EXISTS channel VARCHAR(32)" in statements
    assert "agent_runs ADD COLUMN IF NOT EXISTS external_id VARCHAR(128)" in statements
    assert "agent_runs ADD COLUMN IF NOT EXISTS origin_metadata JSONB" in statements
    assert "agent_run_requests ADD COLUMN IF NOT EXISTS channel VARCHAR(32)" in statements
    assert "agent_run_requests ADD COLUMN IF NOT EXISTS external_id VARCHAR(128)" in statements
    assert "agent_run_requests ADD COLUMN IF NOT EXISTS origin_metadata JSONB" in statements


@pytest.mark.asyncio
async def test_ensure_business_schema_backfills_historical_organization_snapshots_idempotently():
    """旧历史事件应只按当前组织关系推算一次，并保留明确标记。"""

    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)
    for table_name in (
        "conversations",
        "tool_calls",
        "message_feedbacks",
        "operation_logs",
        "security_audits",
    ):
        assert table_name in statements
    assert "organization_id_snapshot INTEGER" in statements
    assert "organization_path_snapshot VARCHAR(512)" in statements
    assert "organization_snapshot_inferred = TRUE" in statements
    assert "organization_snapshot_inferred IS NULL" in statements
    assert "ALTER COLUMN organization_snapshot_inferred SET DEFAULT FALSE" in statements
    assert statements.index("ADD COLUMN IF NOT EXISTS organization_id_snapshot") < statements.index(
        "organization_snapshot_inferred = TRUE"
    )


@pytest.mark.asyncio
async def test_ensure_business_schema_backfills_resource_creation_snapshots_idempotently():
    """旧资源只按创建者当前组织回填一次，并标记为推算。"""

    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)
    for table_name in ("knowledge_bases", "agents", "skills"):
        assert f"UPDATE {table_name} AS resource" in statements
    assert "resource.created_by = users.uid" in statements
    assert "resource.organization_snapshot_inferred IS NULL" in statements
    assert "ARRAY['knowledge_bases', 'agents', 'skills']" in statements


@pytest.mark.asyncio
async def test_ensure_business_schema_removes_unbound_api_keys_before_requiring_user_id():
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)

    assert "UPDATE cli_auth_sessions" in statements
    assert "DELETE FROM api_keys WHERE user_id IS NULL" in statements
    assert "ALTER TABLE IF EXISTS api_keys ALTER COLUMN user_id SET NOT NULL" in statements
    assert statements.index("UPDATE cli_auth_sessions") < statements.index("DELETE FROM api_keys WHERE user_id IS NULL")
    assert statements.index("DELETE FROM api_keys WHERE user_id IS NULL") < statements.index(
        "ALTER TABLE IF EXISTS api_keys ALTER COLUMN user_id SET NOT NULL"
    )


@pytest.mark.asyncio
async def test_share_config_migration_wraps_legacy_scopes_as_read_only():
    """Agent/skill 迁移只把旧 scope 写入 read_scope，manage_scope 置空，避免把历史只读/使用权限追溯升级为 MANAGE。"""
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
        await manager.ensure_knowledge_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)
    assert "UPDATE agents SET share_config = jsonb_build_object" in statements
    assert "UPDATE skills SET share_config = jsonb_build_object" in statements
    assert "UPDATE knowledge_bases SET share_config = jsonb_build_object" in statements
    assert "'read_scope'" in statements
    assert "'manage_scope', NULL" in statements
    assert "ALTER TABLE IF EXISTS agents ALTER COLUMN share_config TYPE JSONB USING share_config::jsonb" in statements
    assert "ALTER TABLE IF EXISTS skills ALTER COLUMN share_config TYPE JSONB USING share_config::jsonb" in statements
    assert statements.index(
        "ALTER TABLE IF EXISTS agents ALTER COLUMN share_config TYPE JSONB USING share_config::jsonb"
    ) < statements.index("UPDATE agents SET share_config = jsonb_build_object")
    assert statements.index(
        "ALTER TABLE IF EXISTS skills ALTER COLUMN share_config TYPE JSONB USING share_config::jsonb"
    ) < statements.index("UPDATE skills SET share_config = jsonb_build_object")
    assert "ALTER TABLE IF EXISTS agents ALTER COLUMN share_config DROP DEFAULT" in statements
    assert "ALTER TABLE IF EXISTS skills ALTER COLUMN share_config DROP DEFAULT" in statements


@pytest.mark.asyncio
async def test_ensure_knowledge_schema_rebuilds_vectors_for_incomplete_legacy_chunks():
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_knowledge_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)

    assert (
        "UPDATE knowledge_chunks SET graph_structure_indexed = TRUE "
        "WHERE graph_indexed IS TRUE AND graph_structure_indexed IS NOT TRUE"
    ) in statements
    assert "mention.entity_id = entity.entity_id AND chunk.graph_indexed IS NOT TRUE" in statements
    assert "mention.triple_id = triple.triple_id AND chunk.graph_indexed IS NOT TRUE" in statements
    assert "THEN 'pending' ELSE 'indexed'" in statements
