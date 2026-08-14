from __future__ import annotations

from yuxi.permissions.role_catalog import BUILTIN_ROLES, DATA_SCOPE_CATALOG, PERMISSION_CATALOG


def test_role_catalog_has_stable_keys_and_complete_builtin_mappings():
    """权限目录标识必须唯一，内置角色只能引用目录中的功能权限。"""
    permission_keys = [item.key for item in PERMISSION_CATALOG]
    scope_keys = {item.key for item in DATA_SCOPE_CATALOG}

    assert len(permission_keys) == len(set(permission_keys))
    assert all(key.count(":") == 1 for key in permission_keys)
    assert scope_keys == {
        "none",
        "self",
        "organization_and_descendants",
        "selected_organizations_and_descendants",
        "all",
    }

    roles = {role.code: role for role in BUILTIN_ROLES}
    assert set(roles) == {"superadmin", "admin", "user"}
    assert set(roles["superadmin"].permission_keys) == set(permission_keys)
    assert set(roles["admin"].permission_keys) < set(permission_keys)
    assert set(roles["user"].permission_keys) < set(roles["admin"].permission_keys)
    assert all(set(role.permission_keys) <= set(permission_keys) for role in BUILTIN_ROLES)
    assert roles["superadmin"].default_scope_type == "all"
    assert roles["admin"].default_scope_type == "organization_and_descendants"
    assert roles["user"].default_scope_type == "self"
