from types import SimpleNamespace

import pytest
from starlette.requests import Request
from yuxi.permissions.authorization import AuthorizationTarget, build_authorization_context

from server.utils.auth_middleware import get_authorization_context


def _assignment(permission_keys, scope_type, department_ids=()):
    """构造最小角色分配测试对象。"""

    role = SimpleNamespace(
        is_active=True,
        permissions=[SimpleNamespace(permission_key=key) for key in permission_keys],
        default_scope_type=scope_type,
        default_departments=[SimpleNamespace(department_id=value) for value in department_ids],
    )
    return SimpleNamespace(role=role, scope_mode="inherit", scope_departments=[])


def _context(*assignments):
    """用指定角色分配构造授权上下文。"""

    user = SimpleNamespace(id=7, department_id=2, role_assignments=list(assignments))
    return build_authorization_context(user)


def test_permission_and_data_scope_cannot_be_spliced_across_assignments():
    """功能权限与数据范围不能跨角色拼接。"""

    context = _context(
        _assignment(["user:read"], "none"),
        _assignment(["dashboard:view"], "all"),
    )

    assert context.allows("user:read", AuthorizationTarget(owner_user_id=7)) is False


def test_complete_assignments_are_combined_as_allow_union():
    """多条完整角色分配按允许集合合并。"""

    context = _context(
        _assignment(["user:read"], "selected_organizations_and_descendants", [2]),
        _assignment(["user:read"], "selected_organizations_and_descendants", [4]),
    )

    assert context.allows("user:read", AuthorizationTarget(department_ancestor_ids=(1, 2, 3))) is True
    assert context.allows("user:read", AuthorizationTarget(department_ancestor_ids=(1, 4, 5))) is True
    assert context.allows("user:read", AuthorizationTarget(department_ancestor_ids=(1, 6))) is False


def test_unassigned_permission_is_denied_by_default():
    """未分配的功能权限默认拒绝。"""

    context = _context(_assignment(["agent:use"], "self"))

    assert context.has_permission("role:read") is False
    assert context.allows("role:read") is False


def test_permission_scopes_keep_each_assignment_intact():
    """历史查询只能读取真正授予该功能权限的分配范围。"""

    context = _context(
        _assignment(["dashboard:view"], "selected_organizations_and_descendants", [2]),
        _assignment(["user:read"], "all"),
    )

    assert context.permission_scopes("dashboard:view") == (
        ("selected_organizations_and_descendants", frozenset({2})),
    )


@pytest.mark.asyncio
async def test_authorization_context_is_reused_only_within_request():
    """授权上下文仅在同一请求内复用。"""

    user = SimpleNamespace(
        id=7,
        department_id=2,
        role_assignments=[_assignment(["role:read"], "self")],
    )
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    first = await get_authorization_context(request, user)
    second = await get_authorization_context(request, user)
    other_request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    assert second is first
    assert await get_authorization_context(other_request, user) is not first
