from __future__ import annotations

from yuxi.permissions.role_catalog import BUILTIN_ROLES, DATA_SCOPE_CATALOG, PERMISSION_CATALOG


def test_role_catalog_has_stable_keys_and_complete_builtin_mappings():
    """权限目录标识必须唯一，内置角色只能引用目录中的功能权限。"""
    permission_keys = [item.key for item in PERMISSION_CATALOG]
    scope_keys = {item.key for item in DATA_SCOPE_CATALOG}

    assert len(permission_keys) == len(set(permission_keys))
    assert all(key.count(":") == 1 for key in permission_keys)
    assert "agent:use" not in permission_keys
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


def test_role_catalog_operations_reference_menu_permissions():
    """操作权限必须挂在同分组的菜单权限下。"""
    permissions = {item.key: item for item in PERMISSION_CATALOG}
    menus = {key: item for key, item in permissions.items() if item.parent_key is None}

    assert set(menus) >= {"dashboard:view", "user:read", "department:read", "role:read"}
    assert permissions["agent:manage"].parent_key is None
    assert all(menu.display_order > 0 for menu in menus.values())
    assert len({menu.display_order for menu in menus.values()}) == len(menus)
    for permission in PERMISSION_CATALOG:
        if permission.parent_key is None:
            continue

        assert permission.parent_key in menus
        assert menus[permission.parent_key].group == permission.group
