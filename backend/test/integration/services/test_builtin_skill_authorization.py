"""内置 Skill 首次安装与重同步使用相同的资源授权契约。"""

import pytest
from sqlalchemy import select

from test.integration.support.module_audit import isolated_audit_store
from yuxi.modules.agents.runtime.agent_backends.chatbot.context import ChatBotContext
from yuxi.modules.agents.runtime.context import refresh_agent_context_resources
from yuxi.modules.extensions.skills.builtin.sync import init_builtin_skills
from yuxi.modules.extensions.skills.models import Skill
from yuxi.modules.extensions.skills.repository import SkillRepository
from yuxi.modules.identity.models import Role, RolePermission, User, UserRoleAssignment
from yuxi.modules.identity.repositories.users import UserRepository

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


async def test_first_install_and_resync_keep_builtin_skill_usable_without_enabling_disabled(monkeypatch, tmp_path):
    """真实首次安装可预加载知识技能，重同步修复旧格式且不改变管理员禁用状态。"""
    monkeypatch.setenv("YUXI_SKILL_DATA_DIR", str(tmp_path / "sources"))
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("YUXI_SKILL_PROJECTION_DIR", str(tmp_path / "projections"))
    async with isolated_audit_store(monkeypatch) as (_, sessions):
        async with sessions() as db:
            role = Role(
                code="skill-reader", name="Reader", default_scope_type="self", permissions=[RolePermission(permission_key="skill:use")]
            )
            db.add(User(uid="reader", username="reader", password_hash="fixture", role_assignments=[UserRoleAssignment(role=role)]))
            await db.commit()
            await init_builtin_skills(db)
        async with sessions() as db:
            user = await UserRepository(db).get_by_uid("reader")
            readable = {skill.slug for skill in await SkillRepository(db).list_enabled_readable(user)}
            assert "knowledge-base" in readable
            context = ChatBotContext(
                uid="reader",
                thread_id="fixture",
                skills=["knowledge-base"],
                preload_skills=["knowledge-base"],
                tools=[],
                mcps=[],
                knowledges="all",
            )
            await refresh_agent_context_resources(context, db=db, user=user)
            assert context.skill_runtime_snapshot["preloaded_skills"] == ["knowledge-base"]
            assert context.skill_runtime_snapshot["runtime_skills"]["knowledge-base"]["mcps"] == ["knowledge"]
            assert "query_kb" in context.skill_runtime_snapshot["preloaded_skill_contents"]["knowledge-base"]
            skill = await db.scalar(select(Skill).where(Skill.slug == "knowledge-base"))
            skill.share_config = {"access_level": "global", "department_ids": [], "user_uids": []}
            disabled = await db.scalar(select(Skill).where(Skill.slug == "deep-research"))
            disabled.enabled = False
            await db.commit()
            await init_builtin_skills(db)
        async with sessions() as db:
            user = await UserRepository(db).get_by_uid("reader")
            readable = {skill.slug for skill in await SkillRepository(db).list_enabled_readable(user)}
            assert "knowledge-base" in readable
            assert "deep-research" not in readable
            skill = await db.scalar(select(Skill).where(Skill.slug == "knowledge-base"))
            assert skill.share_config == {
                "version": 2,
                "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
                "manage_scope": None,
            }
