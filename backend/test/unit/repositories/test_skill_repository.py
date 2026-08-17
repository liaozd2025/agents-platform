from unittest.mock import AsyncMock

import pytest

from yuxi.agents.skills.repository import SkillRepository

pytestmark = pytest.mark.unit


class _FakeDb:
    """记录 SkillRepository 创建行为的最小数据库替身。"""

    def __init__(self):
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    def add(self, item):
        self.item = item


@pytest.mark.asyncio
async def test_builtin_skill_creation_does_not_resolve_system_as_user(monkeypatch):
    """内置 Skill 的系统创建者没有组织，不应查询不存在的用户。"""

    captured_uids = []

    async def snapshot(_db, *, uid=None, **_kwargs):
        captured_uids.append(uid)
        return {
            "organization_id_snapshot": None,
            "organization_path_snapshot": None,
            "organization_snapshot_inferred": False,
        }

    monkeypatch.setattr("yuxi.agents.skills.repository.get_user_organization_snapshot", snapshot)
    repository = SkillRepository(_FakeDb())
    skill = await repository.create(
        slug="builtin-test",
        name="Builtin",
        description="Builtin test",
        source_type="builtin",
        tool_dependencies=[],
        mcp_dependencies=[],
        skill_dependencies=[],
        dir_path="skills/builtin-test",
        share_config={"version": 2, "read_scope": {"access_level": "global"}, "manage_scope": None},
        created_by="system",
    )

    assert captured_uids == [None]
    assert skill.created_by == "system"
