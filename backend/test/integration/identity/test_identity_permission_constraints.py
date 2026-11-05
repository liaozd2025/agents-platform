"""真实 PostgreSQL 验证多角色身份边界和最后真人管理员保护。"""

import asyncio
import os
import uuid

import asyncpg
import pytest

from yuxi.migrations.schema import IDENTITY_PERMISSION_STATEMENTS


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.self_managed_resources
@pytest.mark.parametrize("pending_change", ["delete_user", "revoke_role"])
async def test_app_role_rejection_and_concurrent_last_human_admin_protection(pending_change):
    """直接 SQL 不能给 APP 角色；并发删除和撤权在取得行锁前串行化。"""
    schema = f"permission_{uuid.uuid4().hex}"
    dsn = os.environ["POSTGRES_URL"].replace("+asyncpg", "")
    connections = [await asyncpg.connect(dsn) for _ in range(3)]
    owner, first, second = connections
    try:
        await owner.execute(f'CREATE SCHEMA "{schema}"')
        for connection in connections:
            await connection.execute(f'SET search_path TO "{schema}"')
        await owner.execute("CREATE TABLE users (id int PRIMARY KEY, user_kind text NOT NULL, is_deleted int NOT NULL)")
        await owner.execute("CREATE TABLE roles (id int PRIMARY KEY, code text UNIQUE NOT NULL, is_active boolean NOT NULL)")
        await owner.execute(
            "CREATE TABLE user_role_assignments (id int PRIMARY KEY, user_id int REFERENCES users(id), "
            "role_id int REFERENCES roles(id), UNIQUE(user_id, role_id))"
        )
        for statement in IDENTITY_PERMISSION_STATEMENTS:
            await owner.execute(statement)
        await owner.execute("INSERT INTO users VALUES (1, 'human', 0), (2, 'human', 0), (3, 'end_user', 0)")
        await owner.execute("INSERT INTO roles VALUES (1, 'superadmin', true), (2, 'user', true)")
        await owner.execute("INSERT INTO user_role_assignments VALUES (1, 1, 1), (2, 2, 1)")
        with pytest.raises(asyncpg.CheckViolationError, match="APP"):
            await owner.execute("INSERT INTO user_role_assignments VALUES (3, 3, 1)")
        assert await owner.fetchval("SELECT count(*) FROM user_role_assignments WHERE user_id=3") == 0
        with pytest.raises(asyncpg.CheckViolationError, match="APP"):
            await owner.execute("UPDATE users SET user_kind='end_user' WHERE id=1")

        # fork 允许在仍有其他管理员时调整角色，禁止的是丢失最后一位真人管理员。
        await owner.execute("UPDATE user_role_assignments SET role_id=2 WHERE id=2")
        await owner.execute("UPDATE user_role_assignments SET role_id=1 WHERE id=2")
        transaction = first.transaction()
        await transaction.start()
        committed = False
        await first.execute("UPDATE users SET is_deleted=1 WHERE id=1")
        query = (
            "UPDATE users SET is_deleted=1 WHERE id=2"
            if pending_change == "delete_user"
            else "UPDATE user_role_assignments SET role_id=2 WHERE id=2"
        )
        pending = asyncio.create_task(second.execute(query))
        try:
            async with asyncio.timeout(5):
                while not await owner.fetchval(
                    "SELECT wait_event = 'advisory' FROM pg_stat_activity WHERE pid=$1", second.get_server_pid()
                ):
                    if pending.done():
                        pytest.fail("第二项身份修改未等待共同事务锁")
                    await asyncio.sleep(0.01)
            # 等待方尚未取得 Role 行锁，删除方仍能按同一方向完成保护查询。
            await asyncio.wait_for(first.fetchval("SELECT id FROM roles WHERE id=1 FOR UPDATE"), 2)
            await transaction.commit()
            committed = True
            with pytest.raises(asyncpg.CheckViolationError, match="保留一个"):
                await asyncio.wait_for(pending, 5)
        finally:
            if not committed:
                await transaction.rollback()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM users u JOIN user_role_assignments a ON a.user_id=u.id JOIN roles r ON r.id=a.role_id "
                "WHERE u.user_kind='human' AND u.is_deleted=0 AND r.code='superadmin' AND r.is_active"
            )
            == 1
        )
        with pytest.raises(asyncpg.CheckViolationError, match="保留一个"):
            await owner.execute("DELETE FROM user_role_assignments WHERE id=2")
        with pytest.raises(asyncpg.CheckViolationError, match="保留一个"):
            await owner.execute("DELETE FROM users WHERE id=2")
        with pytest.raises(asyncpg.CheckViolationError, match="不可删除或停用"):
            await owner.execute("UPDATE roles SET is_active=false WHERE code='superadmin'")
    finally:
        await owner.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        for connection in connections:
            await connection.close()
