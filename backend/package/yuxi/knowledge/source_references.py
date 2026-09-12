"""知识库检索来源的文章级元数据解析。"""

import re
from typing import Any
from urllib.parse import quote, urlencode

OA_ARTICLE_BASE_URL = "https://hnjiudian.cn/web/index.html#/corporate-culture/view-page"
_ARTICLE_METADATA_PATTERN = re.compile(
    r"^\s*(task_id|taskid|oa_id|title|author|author_name|type_name|publish_time|publish_date)"
    r"\s*(?::|：|\t| {2,})\s*(.*?)\s*$",
    re.IGNORECASE,
)


def build_knowledge_source_reference(content: str, *, fallback_title: str = "") -> dict[str, Any]:
    """从文章头部注释构造面向前端的知识库来源。"""
    fields = _parse_article_metadata(content)
    title = fields.get("title") or fallback_title
    task_id = _normalize_task_id(fields.get("task_id") or fields.get("taskid") or fields.get("oa_id"))
    source_ref: dict[str, Any] = {
        "source_type": "knowledge_base",
        "title": title,
    }
    for key in ("author", "author_name", "type_name", "publish_time", "publish_date"):
        if fields.get(key):
            source_ref[key] = fields[key]

    if task_id and title:
        page_type = (
            "1"
            if fields.get("type_name", "").lower()
            in {
                "好文共享",
                "goodarticles",
                "good_articles",
            }
            else "3"
        )
        oa_title = title if page_type == "1" else f"新闻详细-{task_id}"
        # ecType 与 page_type 同源：1=好文共享，3=新闻详细。
        # OA 父插件按 ecType 决定文章详情页的渲染分支，前端内嵌跳转协议同样依赖该参数。
        source_ref["url"] = (
            f"{OA_ARTICLE_BASE_URL}/{page_type}?"
            f"{urlencode([('title', oa_title), ('taskID', task_id), ('ecType', page_type)], quote_via=quote)}"
        )
    return source_ref


async def attach_knowledge_source_references(manager: Any, kb_id: str, kb_name: str, result: Any) -> Any:
    """为一次检索的命中文件补充文章级来源，保留原检索协议。"""
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        return result

    source_refs: dict[str, dict[str, Any]] = {}
    for item in result["results"]:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            item["metadata"] = metadata

        file_id = str(item.get("file_id") or metadata.get("file_id") or "").strip()
        source_ref = source_refs.get(file_id)
        if source_ref is None:
            source_ref = await _load_source_reference(
                manager, kb_id, file_id, str(metadata.get("source") or "").strip()
            )
            source_ref["kb_id"] = kb_id
            source_ref["kb_name"] = kb_name
            source_refs[file_id] = source_ref
        metadata["source_ref"] = source_ref
    return result


def _parse_article_metadata(content: str) -> dict[str, str]:
    """只解析文章开头的固定元数据行，避免把正文当作来源字段。"""
    fields: dict[str, str] = {}
    for line in content.splitlines()[:80]:
        match = _ARTICLE_METADATA_PATTERN.match(line)
        if not match:
            continue
        key = match.group(1).lower()
        value = match.group(2).strip().strip("\"'")
        if value:
            fields[key] = value
    return fields


def _normalize_task_id(value: str | None) -> str:
    """将正文中带引号或浮点格式的 OA 任务号还原为整数标识。"""
    if not value:
        return ""
    task_id = value.strip().strip("\"'")
    match = re.fullmatch(r"(\d+)\.0+", task_id)
    return match.group(1) if match else task_id


async def _load_source_reference(manager: Any, kb_id: str, file_id: str, fallback_title: str) -> dict[str, Any]:
    if not file_id:
        return {"source_type": "knowledge_base", "title": fallback_title}
    try:
        file_info = await manager.get_file_info(kb_id, file_id)
        content = file_info.get("content") if isinstance(file_info, dict) else ""
        if isinstance(content, str):
            return build_knowledge_source_reference(content, fallback_title=fallback_title)
    except Exception:  # 不支持文章读取的外部连接器仍可返回普通来源。
        pass
    return {"source_type": "knowledge_base", "title": fallback_title}
