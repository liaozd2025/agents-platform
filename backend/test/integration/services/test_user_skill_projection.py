"""真实 PostgreSQL 上的用户 Skill 投影并发授权测试。

v0.7.1 离线来源转换不属于当前初始化链；个人工作区保留由当前投影更新和撤权入口验证。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager, suppress

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from yuxi.modules.extensions.skills import projection as projection_service
from yuxi.infrastructure.postgres.manager import pg_manager
from yuxi.modules.extensions.skills.models import Skill
from yuxi.modules.identity.models import Department
from yuxi.modules.identity.repositories.users import UserRepository
from yuxi.bootstrap.identity import ensure_builtin_roles
from yuxi.infrastructure.postgres.base import BusinessBase
from yuxi.infrastructure.postgres.models import load_models
from yuxi.modules.workspace.paths import user_workspace_dir

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


@pytest_asyncio.fixture
async def skill_sessions(tmp_path, monkeypatch):
    """每例使用随机 Schema、真实 RBAC 和临时文件根，不触碰 public 用户或 Skill。"""
    schema = "skill_projection_" + uuid.uuid4().hex
    admin = create_async_engine(os.environ["POSTGRES_URL"])
    engine = create_async_engine(os.environ["POSTGRES_URL"], connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    load_models()

    @asynccontextmanager
    async def local_session_context():
        async with factory() as db, db.begin():
            yield db

    monkeypatch.setattr(pg_manager, "get_async_session_context", local_session_context)
    monkeypatch.setattr(projection_service, "get_skill_data_dir", lambda: tmp_path / "skill-sources")
    monkeypatch.setattr(projection_service, "get_skill_projection_dir", lambda: tmp_path / "skill-projections")
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path / "threads"))
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            assert await connection.scalar(text("SELECT current_schema()")) == schema
            await connection.run_sync(BusinessBase.metadata.create_all)
        async with factory() as db:
            db.add(Department(id=1, name="集团", node_type="group", path="/1/"))
            await db.flush()
            await ensure_builtin_roles(db)
            await db.commit()
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


async def _wait_for_advisory_waiter(session_factory, lock_identity) -> bool:
    """等待真实 PostgreSQL 观察到同一 advisory lock 的阻塞者。"""
    deadline = asyncio.get_running_loop().time() + 5
    async with session_factory() as observer_db:
        while asyncio.get_running_loop().time() < deadline:
            waiting = bool(
                await observer_db.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE locktype = 'advisory' AND NOT granted "
                        "AND classid::bigint = :classid "
                        "AND objid::bigint = :objid AND objsubid = :objsubid)"
                    ),
                    {
                        "classid": lock_identity.classid,
                        "objid": lock_identity.objid,
                        "objsubid": lock_identity.objsubid,
                    },
                )
            )
            if waiting:
                return True
            await asyncio.sleep(0.01)
    return False


async def test_projection_refresh_waits_for_lock_then_reloads_revoked_authorization(tmp_path, skill_sessions):
    """等待 uid 锁的 refresh 必须在取锁后重读最新授权。"""
    session_factory = skill_sessions

    suffix = uuid.uuid4().hex
    uid = f"pytest-skill-user-{suffix}"
    slug = f"pytest-skill-{suffix}"
    source_dir = tmp_path / "skill-sources/shared" / slug
    source_dir.mkdir(parents=True)
    (source_dir / "SKILL.md").write_text("# authorized\n", encoding="utf-8")
    skill_id: int | None = None
    refresh_task: asyncio.Task[dict[str, str]] | None = None
    policy_task: asyncio.Task[None] | None = None
    lock_scope = f"yuxi:skills:user-projection:v1:{uid}"

    try:
        async with session_factory() as db:
            await UserRepository(db).create_with_db(db, {"username": uid, "uid": uid, "password_hash": "test", "department_id": 1})
            skill = Skill(
                slug=slug,
                name=slug,
                description="PostgreSQL advisory lock integration fixture",
                source_type="upload",
                tool_dependencies=[],
                mcp_dependencies=[],
                skill_dependencies=[],
                dir_path=f"shared/{slug}",
                share_config={
                    "version": 2,
                    "read_scope": {
                        "access_level": "user",
                        "department_ids": [],
                        "user_uids": [uid],
                    },
                    "manage_scope": None,
                },
                enabled=True,
                created_by="another-user",
            )
            db.add(skill)
            await db.commit()
            skill_id = skill.id

        projection_service.sync_user_accessible_skills(uid, {slug: source_dir})
        projection = projection_service.get_user_skills_root_dir(uid)
        assert (projection / slug / "SKILL.md").is_file()

        source_file = source_dir / "SKILL.md"
        async with session_factory() as edit_db:
            await edit_db.execute(select(Skill).where(Skill.id == skill_id).with_for_update())
            source_file.write_text("# uncommitted\n", encoding="utf-8")
            try:
                refresh_task = asyncio.create_task(projection_service.refresh_user_skill_projection_async(uid))
                await asyncio.sleep(0.2)
                assert not refresh_task.done(), "投影不得读取尚未提交的 Skill 文件"
                assert (projection / slug / "SKILL.md").read_text(encoding="utf-8") == "# authorized\n"
            finally:
                source_file.write_text("# authorized\n", encoding="utf-8")
                await edit_db.rollback()

        await asyncio.wait_for(refresh_task, timeout=5)
        assert (projection / slug / "SKILL.md").read_text(encoding="utf-8") == "# authorized\n"

        async with session_factory() as db:
            await db.execute(update(Skill).where(Skill.id == skill_id).values(enabled=False))
            await db.commit()
        await projection_service.refresh_user_skill_projection_async(uid)
        assert not (projection / slug).exists()

        async with session_factory() as lock_db:
            await lock_db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_scope))"),
                {"lock_scope": lock_scope},
            )
            lock_identity = (
                await lock_db.execute(
                    text(
                        "SELECT classid::bigint, objid::bigint, objsubid FROM pg_locks "
                        "WHERE pid = pg_backend_pid() AND locktype = 'advisory' AND granted"
                    )
                )
            ).one()
            try:
                refresh_task = asyncio.create_task(projection_service.refresh_user_skill_projection_async(uid))
                assert await _wait_for_advisory_waiter(session_factory, lock_identity)
                async with session_factory() as checker:
                    await checker.execute(text("SET LOCAL lock_timeout = '200ms'"))
                    with pytest.raises(DBAPIError, match="lock timeout"):
                        await checker.execute(select(Skill).where(Skill.id == skill_id).with_for_update())
                    await checker.rollback()
            finally:
                await lock_db.commit()

        await asyncio.wait_for(refresh_task, timeout=5)
        async with session_factory() as db:
            await db.execute(update(Skill).where(Skill.id == skill_id).values(enabled=True))
            await db.commit()
        await projection_service.refresh_user_skill_projection_async(uid)
        assert (projection / slug / "SKILL.md").is_file()

        async with session_factory() as policy_db:
            await policy_db.execute(
                update(Skill)
                .where(Skill.id == skill_id)
                .values(
                    share_config={
                        "version": 2,
                        "read_scope": {
                            "access_level": "user",
                            "department_ids": [],
                            "user_uids": ["different-user"],
                        },
                        "manage_scope": None,
                    }
                )
            )
            refresh_task = asyncio.create_task(projection_service.refresh_user_skill_projection_async(uid))
            await asyncio.sleep(0.2)
            assert not refresh_task.done()
            policy_task = asyncio.create_task(projection_service.commit_skill_policy_and_refresh_projections(policy_db, slug))
            await asyncio.wait_for(policy_task, timeout=5)

        refreshed_sources = await asyncio.wait_for(refresh_task, timeout=5)
        assert slug not in refreshed_sources
        assert not (projection / slug).exists()

        async with session_factory() as db:
            await db.execute(
                update(Skill)
                .where(Skill.id == skill_id)
                .values(
                    share_config={
                        "version": 2,
                        "read_scope": {
                            "access_level": "user",
                            "department_ids": [],
                            "user_uids": [uid],
                        },
                        "manage_scope": None,
                    }
                )
            )
            await db.commit()
        await projection_service.refresh_user_skill_projection_async(uid)
        assert (projection / slug / "SKILL.md").is_file()

        async with session_factory() as lock_db:
            await lock_db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_scope))"),
                {"lock_scope": lock_scope},
            )
            lock_identity = (
                await lock_db.execute(
                    text(
                        "SELECT classid::bigint, objid::bigint, objsubid FROM pg_locks "
                        "WHERE pid = pg_backend_pid() AND locktype = 'advisory' AND granted"
                    )
                )
            ).one()
            async with session_factory() as policy_db:
                await policy_db.execute(
                    update(Skill)
                    .where(Skill.id == skill_id)
                    .values(
                        share_config={
                            "version": 2,
                            "read_scope": {
                                "access_level": "user",
                                "department_ids": [],
                                "user_uids": ["different-user"],
                            },
                            "manage_scope": None,
                        }
                    )
                )
                policy_task = asyncio.create_task(projection_service.commit_skill_policy_and_refresh_projections(policy_db, slug))
                assert await _wait_for_advisory_waiter(session_factory, lock_identity), (
                    "policy mutation did not wait on the uid projection lock"
                )
                assert not policy_task.done()
                assert (projection / slug / "SKILL.md").is_file()
                await lock_db.commit()
                await asyncio.wait_for(policy_task, timeout=5)

        assert not (projection / slug).exists()
        async with session_factory() as db:
            persisted_share_config = await db.scalar(select(Skill.share_config).where(Skill.id == skill_id))
        assert persisted_share_config["read_scope"]["user_uids"] == ["different-user"]
    finally:
        for task in (refresh_task, policy_task):
            if task is None:
                continue
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task


async def test_shared_projection_refresh_and_revocation_preserve_personal_workspace(tmp_path, skill_sessions):
    """当前共享投影的替换、撤权和重复刷新均不删除同名个人 Skill 或工作文件。"""
    uid = "projection-personal-" + uuid.uuid4().hex
    slug = "shared-personal-collision"
    shared = tmp_path / "skill-sources/shared" / slug
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text("# shared version one\n")
    personal = user_workspace_dir(uid) / "agents/skills" / slug / "SKILL.md"
    personal.parent.mkdir(parents=True)
    personal.write_bytes(b"personal-skill-preserved\n")
    document = user_workspace_dir(uid) / "projects/keep/report.txt"
    document.parent.mkdir(parents=True)
    document.write_bytes(b"private-project-content\n")
    expected = {path: (path.read_bytes(), path.stat().st_ino) for path in (personal, document)}
    async with skill_sessions() as db:
        await UserRepository(db).create_with_db(db, {"username": uid, "uid": uid, "password_hash": "test", "department_id": 1})
        skill = Skill(
            slug=slug,
            name=slug,
            description="projection fixture",
            source_type="upload",
            dir_path=f"shared/{slug}",
            share_config={
                "version": 2,
                "read_scope": {"access_level": "user", "department_ids": [], "user_uids": [uid]},
                "manage_scope": None,
            },
            enabled=True,
            created_by="different-owner",
        )
        db.add(skill)
        await db.commit()
        skill_id = skill.id

    await projection_service.refresh_user_skill_projection_async(uid)
    projected = projection_service.get_user_skills_root_dir(uid) / slug
    assert (projected / "SKILL.md").read_text() == "# shared version one\n"
    (shared / "SKILL.md").write_text("# shared version two\n")
    await projection_service.refresh_user_skill_projection_async(uid)
    assert (projected / "SKILL.md").read_text() == "# shared version two\n"
    for path, (content, inode) in expected.items():
        assert path.read_bytes() == content and path.stat().st_ino == inode

    async with skill_sessions() as db:
        await db.execute(update(Skill).where(Skill.id == skill_id).values(enabled=False))
        await projection_service.commit_skill_policy_and_refresh_projections(db, slug)
    for _ in range(2):
        await projection_service.refresh_user_skill_projection_async(uid)
        assert not projected.exists()
        for path, (content, inode) in expected.items():
            assert path.read_bytes() == content and path.stat().st_ino == inode
    async with skill_sessions() as db:
        assert await db.scalar(select(Skill.enabled).where(Skill.id == skill_id)) is False
    assert (shared / "SKILL.md").read_text() == "# shared version two\n"
