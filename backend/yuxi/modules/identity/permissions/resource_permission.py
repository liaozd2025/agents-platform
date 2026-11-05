"""统一解析 Agent、Skill 与知识库的共享权限。"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from enum import StrEnum
from typing import Any, Protocol

from yuxi.infrastructure.observability.logging import logger

ADMIN_ROLES = frozenset({"admin", "superadmin"})


class ResourcePermission(StrEnum):
    """资源权限等级，数值顺序用于判断权限是否足够。"""

    NONE = "none"
    READ = "read"
    MANAGE = "manage"


class ResourcePermissionDenied(PermissionError):
    """当前用户的资源权限不足。"""


class ShareableResource(Protocol):
    """声明可通过共享配置进行权限解析的资源字段。"""

    created_by: str | None
    share_config: dict | None


RESOURCE_PERMISSION_ORDER = {
    ResourcePermission.NONE: 0,
    ResourcePermission.READ: 1,
    ResourcePermission.MANAGE: 2,
}

DEFAULT_SCOPE = {"access_level": "global", "department_ids": [], "user_uids": []}


def _normalize_scope(scope: dict | None) -> dict | None:
    """规范化共享范围并校验其访问级别与成员列表。"""

    if scope is None:
        return None
    if not isinstance(scope, dict):
        raise ValueError("权限范围必须是对象")

    access_level = scope.get("access_level")
    if access_level is not None and not isinstance(access_level, str):
        raise ValueError("资源权限访问级别必须是字符串")
    access_level = access_level or "global"
    if access_level not in {"global", "department", "user"}:
        raise ValueError("无效的资源权限范围")

    if access_level == "global":
        return DEFAULT_SCOPE.copy()
    if access_level == "department":
        members = scope.get("department_ids")
        if not isinstance(members, list) or any(type(value) not in (int, str) for value in members):
            raise ValueError("部门权限成员必须是整数或整数字符串组成的列表")
        department_ids = sorted({int(value) for value in members})
        if not department_ids:
            raise ValueError("部门权限至少需要选择一个部门")
        return {"access_level": access_level, "department_ids": department_ids, "user_uids": []}

    members = scope.get("user_uids")
    if not isinstance(members, list) or any(not isinstance(value, str) for value in members):
        raise ValueError("用户权限成员必须是字符串列表")
    user_uids = sorted({value.strip() for value in members if value.strip()})
    if not user_uids:
        raise ValueError("指定用户权限至少需要选择一个用户")
    return {"access_level": access_level, "department_ids": [], "user_uids": user_uids}


def get_permission_department_ids(share_config: dict | None) -> set[int]:
    """提取共享配置中保存校验所需的组织节点 ID。"""

    if not isinstance(share_config, dict) or share_config.get("version") != 2:
        return set()

    return {
        int(department_id)
        for scope_name in ("read_scope", "manage_scope")
        if isinstance(scope := share_config.get(scope_name), dict) and scope.get("access_level") == "department"
        for department_id in scope.get("department_ids") or []
    }


def _validate_department_scope(scope: dict | None, department_paths: Mapping[int, str]) -> None:
    """拒绝同一范围内同时选择上级与下级组织节点。"""

    if not scope or scope["access_level"] != "department":
        return

    selected_ids = scope["department_ids"]
    if any(department_id not in department_paths for department_id in selected_ids):
        raise ValueError("所选组织节点不存在或已删除")

    for index, department_id in enumerate(selected_ids):
        path = department_paths[department_id]
        for other_id in selected_ids[index + 1 :]:
            other_path = department_paths[other_id]
            if path.startswith(other_path) or other_path.startswith(path):
                raise ValueError("同一权限范围不能同时选择上级和下级组织节点")


def _validate_manage_scope(
    read_scope: dict | None,
    manage_scope: dict | None,
    department_paths: Mapping[int, str] | None = None,
) -> None:
    """确保管理范围不会超出读取范围。"""

    if not read_scope or not manage_scope or read_scope["access_level"] == "global":
        return

    read_level = read_scope["access_level"]
    manage_level = manage_scope["access_level"]
    if manage_level != read_level:
        raise ValueError("管理范围必须包含在读取范围内")
    if read_level == manage_level == "department":
        if department_paths is None:
            covered = set(manage_scope["department_ids"]).issubset(read_scope["department_ids"])
        else:
            read_paths = [department_paths[department_id] for department_id in read_scope["department_ids"]]
            covered = all(
                any(department_paths[department_id].startswith(read_path) for read_path in read_paths)
                for department_id in manage_scope["department_ids"]
            )
        if not covered:
            raise ValueError("管理范围必须包含在读取范围内")
    elif read_level == manage_level == "user":
        if not set(manage_scope["user_uids"]).issubset(read_scope["user_uids"]):
            raise ValueError("管理范围必须包含在读取范围内")


def normalize_permission_config(
    share_config: dict | None,
    *,
    allowed_access_levels: Collection[str] | None = None,
    unauthorized_access_level_message: str = "当前用户无权使用该资源共享范围",
    strict: bool = False,
    department_paths: Mapping[int, str] | None = None,
) -> dict:
    """规范化并校验 v2 共享配置。"""

    config = share_config if isinstance(share_config, dict) else {}
    if config.get("version") == 2:
        read_scope = _normalize_scope(config.get("read_scope"))
        manage_scope = _normalize_scope(config.get("manage_scope"))
        try:
            if strict and department_paths is not None:
                _validate_department_scope(read_scope, department_paths)
                _validate_department_scope(manage_scope, department_paths)
            _validate_manage_scope(read_scope, manage_scope, department_paths if strict else None)
        except ValueError:
            if strict:
                raise
            # 读取历史配置时保持原值；保存时由 strict 校验拒绝越界配置。
        normalized = {
            "version": 2,
            "read_scope": read_scope,
            "manage_scope": manage_scope,
        }
        if allowed_access_levels is not None:
            for scope in (normalized["read_scope"], normalized["manage_scope"]):
                if scope and scope["access_level"] not in allowed_access_levels:
                    raise ValueError(unauthorized_access_level_message)
        return normalized
    raise ValueError("资源共享配置必须使用 version 2")


def scope_matches(user: Any, scope: dict | None) -> bool:
    """判断用户是否命中一个共享范围。"""

    if not scope:
        return False
    access_level = scope.get("access_level")
    if access_level == "global":
        return True
    if access_level == "department":
        ancestor_ids = _value(user, "department_ancestor_ids", ()) or ()
        try:
            return not set(map(int, ancestor_ids)).isdisjoint(scope.get("department_ids", []))
        except (TypeError, ValueError):
            return False
    if access_level == "user":
        return str(_value(user, "uid", "") or "") in scope.get("user_uids", [])
    return False


def _value(source: Any, key: str, default: Any = None) -> Any:
    """从字典或对象读取属性，统一权限解析的输入访问方式。"""

    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def resolve_resource_permission(
    user: Any,
    resource: ShareableResource,
) -> ResourcePermission:
    """解析资源所有权和共享范围授予的有效权限。"""

    if _value(user, "is_deleted", 0):
        return ResourcePermission.NONE
    try:
        config = normalize_permission_config(_value(resource, "share_config"))
    except (TypeError, ValueError):
        logger.warning("Invalid resource share config; denying access")
        return ResourcePermission.NONE

    uid = str(_value(user, "uid", "") or "")
    if uid and str(_value(resource, "created_by", "") or "") == uid:
        granted = ResourcePermission.MANAGE
    elif scope_matches(user, config["manage_scope"]) and (config["read_scope"] is None or scope_matches(user, config["read_scope"])):
        granted = ResourcePermission.MANAGE
    elif scope_matches(user, config["read_scope"]):
        granted = ResourcePermission.READ
    else:
        granted = ResourcePermission.NONE
    # APP 终端用户只能消费资源；企业用户的管理资格仍由功能权限与资源范围求交。
    if _value(user, "user_kind") == "end_user" and granted == ResourcePermission.MANAGE:
        return ResourcePermission.READ
    return granted


def require_resource_permission(
    actual: ResourcePermission,
    required: ResourcePermission,
) -> None:
    """在权限不足时显式失败。"""

    if RESOURCE_PERMISSION_ORDER[actual] < RESOURCE_PERMISSION_ORDER[required]:
        raise ResourcePermissionDenied(f"需要 {required.value} 权限，当前为 {actual.value}")


def resolve_knowledge_base_permission(user: Any, resource: ShareableResource) -> ResourcePermission:
    """解析知识库共享范围授予的权限。"""

    return resolve_resource_permission(user, resource)


def require_knowledge_base_permission(
    user: Any,
    resource: ShareableResource,
    required: ResourcePermission,
) -> ResourcePermission:
    """校验用户是否具备知识库所需权限，并返回实际权限。"""

    actual = resolve_knowledge_base_permission(user, resource)
    require_resource_permission(actual, required)
    return actual


def resolve_agent_permission(user: Any, resource: ShareableResource) -> ResourcePermission:
    """私有定义仅供所有者管理，共享定义遵守企业资源范围。"""
    if _value(user, "is_deleted", 0):
        return ResourcePermission.NONE
    if _value(resource, "visibility") == "private":
        if _value(user, "user_kind") == "end_user":
            return ResourcePermission.NONE
        uid = str(_value(user, "uid", "") or "")
        if uid and uid == str(_value(resource, "created_by", "") or ""):
            return ResourcePermission.MANAGE
        return ResourcePermission.NONE
    return resolve_resource_permission(user, resource)


def resolve_skill_permission(user: Any, resource: ShareableResource) -> ResourcePermission:
    """解析个人、专属与共享 Skill 的有效资源权限。"""
    if _value(user, "is_deleted", 0):
        return ResourcePermission.NONE
    if _value(resource, "bound_agent_id") is not None:
        agent = _value(resource, "bound_agent")
        if agent is None:
            return ResourcePermission.NONE
        if _value(user, "user_kind") == "end_user":
            owner = _value(user, "owner_user")
            if owner is None or _value(owner, "is_deleted", 0) or _value(agent, "visibility") != "shared":
                return ResourcePermission.NONE
            permission = resolve_agent_permission(owner, agent)
            return ResourcePermission.READ if permission != ResourcePermission.NONE else ResourcePermission.NONE
        return resolve_agent_permission(user, agent)
    if _value(resource, "source_scope") == "personal":
        if _value(user, "user_kind") == "end_user":
            return ResourcePermission.NONE
        uid = str(_value(user, "uid", "") or "")
        if uid and str(_value(resource, "created_by", "") or "") == uid:
            return ResourcePermission.MANAGE
        return ResourcePermission.NONE
    return resolve_resource_permission(user, resource)
