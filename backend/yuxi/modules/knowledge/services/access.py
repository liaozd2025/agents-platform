"""知识库当前读取权限与调用方选择的交集。"""

from typing import Any, Literal


async def visible_knowledge_bases(
    uid: str,
    *,
    selection: Literal["all"] | list[str] = "all",
) -> list[dict[str, Any]]:
    """按当前用户权限解析全部或显式选择的知识库摘要。"""
    if selection != "all" and (
        not isinstance(selection, list) or any(not isinstance(kb_id, str) or not kb_id.strip() for kb_id in selection)
    ):
        raise ValueError('知识库选择必须是"all" 或非空字符串组成的列表')
    if not uid or selection == []:
        return []

    from yuxi.modules.identity.permissions.authorization import build_authorization_context
    from yuxi.modules.identity.repositories.users import UserRepository
    from yuxi.modules.knowledge.runtime import knowledge_base

    user = await UserRepository().get_by_uid(uid)
    if user is None or user.is_deleted:
        return []
    if getattr(user, "user_kind", "human") == "end_user":
        if not user.app_id or not user.owner_user_id:
            return []
        user = await UserRepository().get_by_id(user.owner_user_id)
        if user is None or user.is_deleted or user.is_login_locked():
            return []
    authorization = build_authorization_context(user)
    if not any(authorization.has_permission(key) for key in ("knowledge_base:read", "knowledge_base:manage")):
        return []

    summaries = await knowledge_base.get_knowledge_bases_by_user(user)
    selected_ids = None if selection == "all" else {kb_id.strip() for kb_id in selection}
    return [
        {"kb_id": item.kb_id, "name": item.name, "description": item.description, "kb_type": item.kb_type}
        for item in summaries
        if selected_ids is None or item.kb_id in selected_ids
    ]
