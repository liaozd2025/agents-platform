"""角色兼容迁移的真实 PostgreSQL 集成测试。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Role, User, UserRoleAssignment

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_role_migration_backfills_each_legacy_role_once_after_repeated_runs():
    """三类旧角色重复迁移后各保留一条分配，旧字段保持不变。"""
    pg_manager.initialize()
    await pg_manager.create_tables()
    await pg_manager.ensure_business_schema()

    suffix = uuid.uuid4().hex[:10]
    legacy_roles = ("superadmin", "admin", "user")
    user_ids: list[int] = []

    try:
        async with pg_manager.get_async_session_context() as session:
            users = [
                User(
                    username=f"pytest-role-{role}-{suffix}",
                    uid=f"pytest_role_{role}_{suffix}",
                    password_hash="integration-test-only",
                    role=role,
                )
                for role in legacy_roles
            ]
            session.add_all(users)
            await session.commit()
            user_ids = [user.id for user in users]

        await pg_manager.ensure_business_schema()
        await pg_manager.ensure_business_schema()

        async with pg_manager.get_async_session_context() as session:
            rows = (
                await session.execute(
                    select(User.id, User.role, Role.code)
                    .join(UserRoleAssignment, UserRoleAssignment.user_id == User.id)
                    .join(Role, Role.id == UserRoleAssignment.role_id)
                    .where(User.id.in_(user_ids))
                    .order_by(User.id)
                )
            ).all()

        assert len(rows) == len(legacy_roles)
        assert {(legacy_role, assigned_role) for _, legacy_role, assigned_role in rows} == {
            (role, role) for role in legacy_roles
        }
    finally:
        if user_ids:
            async with pg_manager.get_async_session_context() as session:
                await session.execute(delete(User).where(User.id.in_(user_ids)))
                await session.commit()
