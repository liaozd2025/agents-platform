"""旧单角色字段收敛迁移的真实 PostgreSQL 集成测试。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select, text

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Role, User, UserRoleAssignment

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_role_migration_backfills_legacy_roles_and_drops_column_idempotently():
    """三类旧角色回填后删除单角色列，重复执行不产生重复分配。"""
    pg_manager.initialize()
    await pg_manager.create_tables()
    await pg_manager.ensure_business_schema()

    suffix = uuid.uuid4().hex[:10]
    legacy_roles = ("superadmin", "admin", "user")
    user_ids: list[int] = []

    try:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR NOT NULL DEFAULT 'user'"))
            for role in legacy_roles:
                user_id = await session.scalar(
                    text(
                        "INSERT INTO users (username, uid, password_hash, role, login_failed_count, is_deleted) "
                        "VALUES (:username, :uid, :password_hash, :role, 0, 0) RETURNING id"
                    ),
                    {
                        "username": f"pytest-role-{role}-{suffix}",
                        "uid": f"pytest_role_{role}_{suffix}",
                        "password_hash": "integration-test-only",
                        "role": role,
                    },
                )
                user_ids.append(user_id)

        await pg_manager.ensure_business_schema()
        await pg_manager.ensure_business_schema()

        async with pg_manager.get_async_session_context() as session:
            rows = (
                await session.execute(
                    select(User.id, Role.code)
                    .join(UserRoleAssignment, UserRoleAssignment.user_id == User.id)
                    .join(Role, Role.id == UserRoleAssignment.role_id)
                    .where(User.id.in_(user_ids))
                    .order_by(User.id)
                )
            ).all()

            role_column_count = await session.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'users' AND column_name = 'role'"
                )
            )

        assert [role_code for _, role_code in rows] == list(legacy_roles)
        assert role_column_count == 0
    finally:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(text("ALTER TABLE users DROP COLUMN IF EXISTS role"))
            if user_ids:
                await session.execute(delete(User).where(User.id.in_(user_ids)))
