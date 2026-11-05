"""知识库选择只收窄当前读取权限。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuxi.modules.knowledge.runtime import knowledge_base
from yuxi.modules.knowledge.services.access import visible_knowledge_bases


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uid", "selection", "expected"),
    [("u1", "all", ["kb-a", "kb-b"]), ("u1", ["kb-a", "private"], ["kb-a"]), ("u1", [], []), ("", "all", [])],
)
async def test_selection_only_narrows_read_permissions(monkeypatch, uid, selection, expected):
    """全部、指定和空选择都不能扩大用户读取范围。"""
    readable = [SimpleNamespace(kb_id=kb_id, name="Docs", description=None, kb_type="milvus") for kb_id in ("kb-a", "kb-b")]
    monkeypatch.setattr(knowledge_base, "get_knowledge_bases_by_user", AsyncMock(return_value=readable))

    result = await visible_knowledge_bases(uid, selection=selection)
    assert [kb["kb_id"] for kb in result] == expected


@pytest.mark.asyncio
async def test_no_knowledge_bases_returns_empty(monkeypatch):
    """没有知识库时默认选择正常返回空范围。"""
    monkeypatch.setattr(knowledge_base, "get_knowledge_bases_by_user", AsyncMock(return_value=[]))
    assert await visible_knowledge_bases("u1") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["kb-a", [" "]])
async def test_invalid_selection_fails(selection):
    """错误类型和空 ID 显式失败，不能扩大为全部资源。"""
    with pytest.raises(ValueError, match="知识库选择必须"):
        await visible_knowledge_bases("u1", selection=selection)


@pytest.fixture(autouse=True)
def readable_user(monkeypatch):
    """使用显式 RBAC 角色，保持共享范围与功能权限分别验证。"""
    from yuxi.modules.identity.repositories.users import UserRepository

    role = SimpleNamespace(
        is_active=True,
        default_scope_type="all",
        default_departments=[],
        permissions=[SimpleNamespace(permission_key="knowledge_base:read")],
    )
    user = SimpleNamespace(
        id=1, uid="u1", is_deleted=False, department_id=1, role_assignments=[SimpleNamespace(role=role, scope_mode="inherit")]
    )
    monkeypatch.setattr(UserRepository, "get_by_uid", AsyncMock(return_value=user))
    return user


@pytest.mark.asyncio
async def test_removed_function_permission_cannot_reuse_visible_selection(monkeypatch, readable_user):
    """MCP/Public 共用入口每次重算功能权限，原资源选择不能绕过撤权。"""
    readable_user.role_assignments = []
    query = AsyncMock(side_effect=AssertionError("无功能权限不得读取知识库"))
    monkeypatch.setattr(knowledge_base, "get_knowledge_bases_by_user", query)
    assert await visible_knowledge_bases("u1", selection=["kb-a"]) == []
    query.assert_not_awaited()


@pytest.mark.asyncio
async def test_end_user_reads_owner_visible_scope_without_exposing_manage_permission(monkeypatch, readable_user):
    """APP 端用户读取 owner 当前资源与选择的交集，输出不携带管理能力。"""
    from yuxi.modules.identity.repositories.users import UserRepository

    end_user = SimpleNamespace(uid="end", is_deleted=False, user_kind="end_user", app_id="app", owner_user_id=1)
    readable_user.is_login_locked = lambda: False
    monkeypatch.setattr(UserRepository, "get_by_uid", AsyncMock(return_value=end_user))
    monkeypatch.setattr(UserRepository, "get_by_id", AsyncMock(return_value=readable_user))
    query = AsyncMock(
        return_value=[
            SimpleNamespace(kb_id="kb-a", name="Docs", description=None, kb_type="milvus"),
            SimpleNamespace(kb_id="other", name="Other", description=None, kb_type="milvus"),
        ]
    )
    monkeypatch.setattr(knowledge_base, "get_knowledge_bases_by_user", query)
    result = await visible_knowledge_bases("end", selection=["kb-a", "hidden"])
    assert result == [{"kb_id": "kb-a", "name": "Docs", "description": None, "kb_type": "milvus"}]
    query.assert_awaited_once_with(readable_user)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_state", ["missing", "deleted", "locked", "revoked"])
async def test_end_user_owner_revocation_closes_knowledge_read(monkeypatch, readable_user, owner_state):
    """owner 缺失、失效或功能撤权后，原 APP 的选择不能继续读取。"""
    from yuxi.modules.identity.repositories.users import UserRepository

    end_user = SimpleNamespace(uid="end", is_deleted=False, user_kind="end_user", app_id="app", owner_user_id=1)
    readable_user.is_login_locked = lambda: owner_state == "locked"
    readable_user.is_deleted = owner_state == "deleted"
    if owner_state == "revoked":
        readable_user.role_assignments = []
    monkeypatch.setattr(UserRepository, "get_by_uid", AsyncMock(return_value=end_user))
    monkeypatch.setattr(UserRepository, "get_by_id", AsyncMock(return_value=None if owner_state == "missing" else readable_user))
    query = AsyncMock(side_effect=AssertionError("失效 owner 不得进入知识资源读取"))
    monkeypatch.setattr(knowledge_base, "get_knowledge_bases_by_user", query)
    assert await visible_knowledge_bases("end", selection=["kb-a"]) == []
    query.assert_not_awaited()
