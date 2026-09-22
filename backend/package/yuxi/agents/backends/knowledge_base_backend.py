from __future__ import annotations

from typing import Any


async def resolve_visible_knowledge_bases_for_context(context) -> list[dict[str, Any]]:
    """实时读取权限，并与智能体、任务和本轮选库范围取交集。"""
    from yuxi.knowledge.runtime import knowledge_base

    uid = getattr(context, "uid", None)
    if not uid:
        setattr(context, "_visible_knowledge_bases", [])
        return []

    summaries = await knowledge_base.get_databases_by_uid(str(uid))
    databases = [
        {
            "kb_id": summary.kb_id,
            "name": summary.name,
            "description": summary.description,
            "kb_type": summary.kb_type,
        }
        for summary in summaries
    ]
    for field_name in ("knowledges", "knowledge_task_scope", "knowledge_selected_kb_ids"):
        selected = getattr(context, field_name, None)
        if selected is not None:
            allowed_ids = set(selected)
            databases = [db for db in databases if db["kb_id"] in allowed_ids]

    setattr(context, "_visible_knowledge_bases", databases)
    return databases
