"""真实PostgreSQL验证组织、管理员与强制审计的原子事务。"""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.bootstrap.identity import ensure_builtin_roles
from yuxi.infrastructure.postgres.base import BusinessBase
from yuxi.infrastructure.postgres.models import load_models
from yuxi.modules.identity.models import Department, User
from yuxi.modules.identity.repositories.departments import DepartmentRepository
from yuxi.modules.identity.repositories.users import UserRepository
from yuxi.modules.identity.services import administration
from yuxi.modules.system.models import OperationLog

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


@asynccontextmanager
async def isolated_identity_database():
    """只操作本用例随机schema，明确准备当前内置角色目录。"""
    load_models()
    schema = f"identity_uow_{uuid.uuid4().hex}"
    admin = create_async_engine(os.environ["POSTGRES_URL"])
    engine = create_async_engine(os.environ["POSTGRES_URL"], connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as conn:
            await conn.run_sync(BusinessBase.metadata.create_all)
        async with factory() as db, db.begin():
            await ensure_builtin_roles(db)
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


async def prepare_actor(factory):
    """创建合法集团根与操作者，供随后独立事务调用。"""
    async with factory() as db, db.begin():
        root = await DepartmentRepository(db).create_group_root(name="测试集团", description=None)
        actor = await UserRepository(db).create(
            {"username": "actor", "uid": "actor", "password_hash": "!test", "department_id": root.id},
            default_role_code="superadmin",
        )
        return actor.id, root.id


async def test_user_conflict_rolls_back_new_department_and_audit():
    """用户唯一性冲突不能留下组织节点或错误审计成功记录。"""
    async with isolated_identity_database() as factory:
        actor_id, root_id = await prepare_actor(factory)
        async with factory() as db, db.begin():
            db.add(User(username="collision_user", uid="different_uid", password_hash="!test", department_id=root_id))
        async with factory() as db:
            with pytest.raises(administration.IdentityConflictError):
                await administration.create_department_with_admin(
                    db,
                    name="失败组织",
                    description=None,
                    admin_uid="collision_user",
                    admin_password="fixture-password",
                    admin_phone=None,
                    actor_user_id=actor_id,
                )
        async with factory() as check:
            assert await check.scalar(select(Department.id).where(Department.name == "失败组织")) is None
            assert await check.scalar(select(User.id).where(User.uid == "collision_user")) is None
            assert await check.scalar(select(func.count(OperationLog.id))) == 0


async def test_required_audit_failure_rolls_back_department_and_admin(monkeypatch):
    """审计失败必须回滚同一用例的组织和管理员。"""
    async with isolated_identity_database() as factory:
        actor_id, _root_id = await prepare_actor(factory)
        monkeypatch.setattr(administration, "log_operation", AsyncMock(side_effect=RuntimeError("audit unavailable")))
        async with factory() as db:
            with pytest.raises(RuntimeError, match="audit unavailable"):
                await administration.create_department_with_admin(
                    db,
                    name="审计失败组织",
                    description=None,
                    admin_uid="audit_candidate",
                    admin_password="fixture-password",
                    admin_phone=None,
                    actor_user_id=actor_id,
                )
        async with factory() as check:
            assert await check.scalar(select(Department.id).where(Department.name == "审计失败组织")) is None
            assert await check.scalar(select(User.id).where(User.uid == "audit_candidate")) is None
            assert await check.scalar(select(func.count(OperationLog.id))) == 0


async def test_concurrent_initialization_has_exactly_one_atomic_winner():
    """两个独立连接初始化时仅一套根组织、用户及审计可以提交。"""
    async with isolated_identity_database() as factory:

        async def initialize(uid):
            async with factory() as db:
                return await administration.initialize_system_admin(db, uid=uid, password="fixture-password", phone_number=None)

        results = await asyncio.gather(initialize("first-admin"), initialize("second-admin"), return_exceptions=True)
        assert sum(isinstance(value, administration.DepartmentAdminCreation) for value in results) == 1
        assert sum(isinstance(value, administration.SystemAlreadyInitializedError) for value in results) == 1
        async with factory() as check:
            assert await check.scalar(select(func.count(User.id))) == 1
            assert await check.scalar(select(func.count(Department.id))) == 1
            assert await check.scalar(select(func.count(OperationLog.id))) == 1
