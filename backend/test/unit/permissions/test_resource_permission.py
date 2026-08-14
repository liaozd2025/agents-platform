from types import SimpleNamespace

import pytest

from yuxi.permissions import (
    ResourcePermission,
    ResourcePermissionDenied,
    require_knowledge_base_permission,
    resolve_agent_permission,
    resolve_knowledge_base_permission,
    resolve_skill_permission,
)


def _user(uid="user-1", department_id=1, department_ancestor_ids=None):
    if department_ancestor_ids is None:
        department_ancestor_ids = [] if department_id is None else [department_id]
    return SimpleNamespace(
        uid=uid,
        department_id=department_id,
        department_ancestor_ids=department_ancestor_ids,
    )


def _resource(created_by="owner", share_config=None):
    return SimpleNamespace(created_by=created_by, share_config=share_config)


def test_knowledge_base_global_read_and_department_manage():
    config = {
        "version": 2,
        "read_scope": {"access_level": "global"},
        "manage_scope": {"access_level": "department", "department_ids": [1]},
    }
    resource = _resource(share_config=config)

    assert resolve_knowledge_base_permission(_user(department_id=1), resource) == ResourcePermission.MANAGE
    managing_admin = _user(uid="admin-1", department_id=1)
    readonly_admin = _user(uid="other", department_id=2)
    assert resolve_knowledge_base_permission(managing_admin, resource) == ResourcePermission.MANAGE
    assert resolve_knowledge_base_permission(readonly_admin, resource) == ResourcePermission.READ


def test_department_scope_inherits_to_descendants_only():
    resource = _resource(
        share_config={
            "version": 2,
            "read_scope": {"access_level": "department", "department_ids": [2]},
            "manage_scope": None,
        }
    )

    assert (
        resolve_knowledge_base_permission(
            _user(department_id=3, department_ancestor_ids=[1, 2, 3]),
            resource,
        )
        == ResourcePermission.READ
    )
    assert (
        resolve_knowledge_base_permission(
            _user(department_id=2, department_ancestor_ids=[1, 2]),
            resource,
        )
        == ResourcePermission.READ
    )
    assert (
        resolve_knowledge_base_permission(
            _user(department_id=1, department_ancestor_ids=[1]),
            resource,
        )
        == ResourcePermission.NONE
    )
    assert (
        resolve_knowledge_base_permission(
            _user(department_id=4, department_ancestor_ids=[1, 4]),
            resource,
        )
        == ResourcePermission.NONE
    )


def test_global_scope_and_group_root_scope_differ_for_unbound_users():
    global_resource = _resource(
        share_config={
            "version": 2,
            "read_scope": {"access_level": "global"},
            "manage_scope": None,
        }
    )
    group_resource = _resource(
        share_config={
            "version": 2,
            "read_scope": {"access_level": "department", "department_ids": [1]},
            "manage_scope": None,
        }
    )
    unbound_user = _user(department_id=None, department_ancestor_ids=[])
    descendant_user = _user(department_id=3, department_ancestor_ids=[1, 2, 3])

    assert resolve_knowledge_base_permission(unbound_user, global_resource) == ResourcePermission.READ
    assert resolve_knowledge_base_permission(unbound_user, group_resource) == ResourcePermission.NONE
    assert resolve_knowledge_base_permission(descendant_user, group_resource) == ResourcePermission.READ


def test_all_resource_types_share_subtree_matching_without_legacy_role_ceilings():
    resource = _resource(
        share_config={
            "version": 2,
            "read_scope": {"access_level": "department", "department_ids": [2]},
            "manage_scope": {"access_level": "department", "department_ids": [2]},
        }
    )
    descendant = _user(department_id=3, department_ancestor_ids=[1, 2, 3])

    assert resolve_knowledge_base_permission(descendant, resource) == ResourcePermission.MANAGE
    assert resolve_agent_permission(descendant, resource) == ResourcePermission.MANAGE
    assert resolve_skill_permission(descendant, resource) == ResourcePermission.MANAGE


def test_invalid_v2_scope_does_not_expand_read_access_when_reading():
    resource = _resource(
        share_config={
            "version": 2,
            "read_scope": {"access_level": "department", "department_ids": [1]},
            "manage_scope": {"access_level": "global"},
        }
    )

    assert resolve_knowledge_base_permission(_user(department_id=2), resource) == ResourcePermission.NONE


def test_strict_config_rejects_manage_scope_outside_read_scope():
    from yuxi.permissions import normalize_permission_config

    with pytest.raises(ValueError, match="管理范围"):
        normalize_permission_config(
            {
                "version": 2,
                "read_scope": {"access_level": "department", "department_ids": [1]},
                "manage_scope": {"access_level": "global"},
            },
            strict=True,
        )


def test_strict_config_rejects_user_manage_scope_under_department_read_scope():
    from yuxi.permissions import normalize_permission_config

    with pytest.raises(ValueError, match="管理范围"):
        normalize_permission_config(
            {
                "version": 2,
                "read_scope": {"access_level": "department", "department_ids": [1]},
                "manage_scope": {"access_level": "user", "user_uids": ["user-1"]},
            },
            strict=True,
        )


def test_strict_config_accepts_manage_department_under_read_ancestor():
    from yuxi.permissions import normalize_permission_config

    config = {
        "version": 2,
        "read_scope": {"access_level": "department", "department_ids": [1]},
        "manage_scope": {"access_level": "department", "department_ids": [2]},
    }

    assert normalize_permission_config(
        config,
        strict=True,
        department_paths={1: "/1/", 2: "/1/2/"},
    ) == {
        "version": 2,
        "read_scope": {"access_level": "department", "department_ids": [1], "user_uids": []},
        "manage_scope": {"access_level": "department", "department_ids": [2], "user_uids": []},
    }


@pytest.mark.parametrize(
    ("read_id", "manage_id"),
    [
        (2, 1),
        (2, 3),
    ],
)
def test_strict_config_rejects_manage_department_outside_read_subtree(read_id, manage_id):
    from yuxi.permissions import normalize_permission_config

    with pytest.raises(ValueError, match="管理范围必须包含在读取范围内"):
        normalize_permission_config(
            {
                "version": 2,
                "read_scope": {"access_level": "department", "department_ids": [read_id]},
                "manage_scope": {"access_level": "department", "department_ids": [manage_id]},
            },
            strict=True,
            department_paths={1: "/1/", 2: "/1/2/", 3: "/1/3/"},
        )


@pytest.mark.parametrize("scope_name", ["read_scope", "manage_scope"])
def test_strict_config_rejects_redundant_department_ancestor(scope_name):
    from yuxi.permissions import normalize_permission_config

    config = {
        "version": 2,
        "read_scope": {"access_level": "global"},
        "manage_scope": None,
    }
    config[scope_name] = {"access_level": "department", "department_ids": [1, 2]}

    with pytest.raises(ValueError, match="同一权限范围不能同时选择上级和下级组织节点"):
        normalize_permission_config(
            config,
            strict=True,
            department_paths={1: "/1/", 2: "/1/2/"},
        )


def test_historical_redundant_department_scope_remains_readable():
    from yuxi.permissions import normalize_permission_config

    config = {
        "version": 2,
        "read_scope": {"access_level": "department", "department_ids": [1, 2]},
        "manage_scope": None,
    }

    assert normalize_permission_config(
        config,
        department_paths={1: "/1/", 2: "/1/2/"},
    )["read_scope"]["department_ids"] == [1, 2]


def test_global_agent_manage_scope_grants_management():
    resource = _resource(
        share_config={
            "version": 2,
            "read_scope": {"access_level": "global"},
            "manage_scope": {"access_level": "global"},
        }
    )

    assert resolve_agent_permission(_user(), resource) == ResourcePermission.MANAGE


def test_user_agent_and_skill_scope_preserves_user_management():
    resource = _resource(
        share_config={
            "version": 2,
            "read_scope": {"access_level": "user", "user_uids": ["user-1"]},
            "manage_scope": {"access_level": "user", "user_uids": ["user-1"]},
        }
    )

    assert resolve_agent_permission(_user(), resource) == ResourcePermission.MANAGE
    assert resolve_skill_permission(_user(), resource) == ResourcePermission.MANAGE


def test_knowledge_base_owner_can_manage_without_global_role_bypass():
    resource = _resource(created_by="owner", share_config={"version": 2})

    assert resolve_knowledge_base_permission(_user(uid="owner"), resource) == ResourcePermission.MANAGE
    assert resolve_knowledge_base_permission(_user(uid="other"), resource) == ResourcePermission.NONE


def test_global_knowledge_base_manage_scope_grants_management():
    resource = _resource(
        share_config={
            "version": 2,
            "read_scope": {"access_level": "global"},
            "manage_scope": {"access_level": "global"},
        }
    )

    assert resolve_knowledge_base_permission(_user(uid="first"), resource) == ResourcePermission.MANAGE
    assert resolve_knowledge_base_permission(_user(uid="second"), resource) == ResourcePermission.MANAGE


def test_legacy_permission_config_is_rejected_at_runtime():
    from yuxi.permissions import normalize_permission_config

    with pytest.raises(ValueError, match="version 2"):
        normalize_permission_config({"access_level": "department", "department_ids": [1]})


def test_agent_and_skill_use_shared_resolver_with_resource_policy():
    resource = _resource(share_config={"version": 2, "manage_scope": {"access_level": "user", "user_uids": ["user-2"]}})

    assert resolve_agent_permission(_user(uid="user-2"), resource) == ResourcePermission.MANAGE
    assert resolve_skill_permission(_user(uid="user-2"), resource) == ResourcePermission.MANAGE


def test_manage_only_scope_also_grants_read_to_matching_users():
    resource = _resource(
        share_config={
            "version": 2,
            "read_scope": None,
            "manage_scope": {"access_level": "department", "department_ids": [1]},
        }
    )

    assert resolve_knowledge_base_permission(_user(department_id=1), resource) == ResourcePermission.MANAGE
    assert resolve_knowledge_base_permission(_user(department_id=1), resource) == ResourcePermission.MANAGE
    assert resolve_knowledge_base_permission(_user(department_id=2), resource) == ResourcePermission.NONE


def test_require_permission_rejects_insufficient_access():
    from yuxi.permissions import require_resource_permission

    with pytest.raises(ResourcePermissionDenied):
        require_resource_permission(ResourcePermission.READ, ResourcePermission.MANAGE)


def test_require_knowledge_base_permission_uses_resolved_resource_permission():
    resource = _resource(
        share_config={
            "version": 2,
            "read_scope": {"access_level": "global"},
            "manage_scope": None,
        }
    )

    assert require_knowledge_base_permission(_user(), resource, ResourcePermission.READ) == ResourcePermission.READ
    with pytest.raises(ResourcePermissionDenied):
        require_knowledge_base_permission(_user(), resource, ResourcePermission.MANAGE)


def test_v2_scope_validation_rejects_disallowed_access_level():
    from yuxi.permissions import normalize_permission_config

    with pytest.raises(ValueError, match="共享范围"):
        normalize_permission_config(
            {
                "version": 2,
                "read_scope": {"access_level": "global"},
                "manage_scope": None,
            },
            allowed_access_levels={"user"},
        )
