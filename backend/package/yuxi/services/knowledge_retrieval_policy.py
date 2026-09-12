"""聊天请求的知识库检索范围与轻量意图策略。"""

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from yuxi.config.runtime import knowledge_capability_enabled

_MENTION_RE = re.compile(r'@knowledge:(?:"((?:\\.|[^"\\])*)"|(\S+))')
_NO_KB_PATTERNS = (
    "你好",
    "您好",
    "谢谢",
    "晚安",
    "早上好",
    "翻译",
    "润色",
    "改写",
    "改成",
    "计算",
    "等于多少",
    "代码格式",
    "格式化代码",
)
_KB_PATTERNS = (
    "知识库",
    "内部资料",
    "内部文档",
    "公司制度",
    "项目资料",
    "项目文档",
    "操作规范",
    "上传的文件",
    "文档中",
    "资料中",
)


@dataclass(frozen=True, slots=True)
class KnowledgeRetrievalDecision:
    """一次请求最终使用的知识库检索决策。"""

    intent: str
    kb_ids: tuple[str, ...]
    mentioned: bool


def parse_knowledge_mentions(query: str) -> tuple[str, ...]:
    """解析消息中的 @knowledge token，返回去重后的值。"""
    values: list[str] = []
    for match in _MENTION_RE.finditer(str(query or "")):
        value = match.group(1) if match.group(1) is not None else match.group(2)
        value = value.replace('\\"', '"').replace("\\\\", "\\")
        if value and value not in values:
            values.append(value)
    return tuple(values)


def classify_knowledge_intent(query: str) -> str:
    """用低成本规则判断是否需要知识库；无法确定时默认检索。"""
    text = str(query or "").strip()
    if not text:
        return "NO_KB"
    if any(pattern in text for pattern in _NO_KB_PATTERNS) and not any(pattern in text for pattern in _KB_PATTERNS):
        return "NO_KB"
    return "SEARCH_KB"


async def decide_knowledge_retrieval(query: str, user: Any) -> KnowledgeRetrievalDecision:
    """解析 @ 范围或动态选择当前用户可读的全局知识库。"""
    if not knowledge_capability_enabled():
        return KnowledgeRetrievalDecision("NO_KB", (), False)
    mentions = parse_knowledge_mentions(query)
    if not mentions and classify_knowledge_intent(query) == "NO_KB":
        return KnowledgeRetrievalDecision("NO_KB", (), False)

    from yuxi.knowledge.runtime import knowledge_base

    summaries = await knowledge_base.get_databases_by_uid(str(user.uid))
    visible = {str(summary.kb_id): summary for summary in summaries}
    if mentions:
        kb_ids = tuple(
            next((kb_id for kb_id, summary in visible.items() if kb_id == value or summary.name == value), value)
            for value in mentions
            if value in visible or any(summary.name == value for summary in visible.values())
        )
        return KnowledgeRetrievalDecision("SEARCH_KB", kb_ids, True)

    global_ids = tuple(
        kb_id
        for kb_id, summary in visible.items()
        if isinstance(summary.share_config, dict)
        and isinstance(summary.share_config.get("read_scope"), dict)
        and summary.share_config["read_scope"].get("access_level") == "global"
    )
    return KnowledgeRetrievalDecision("SEARCH_KB", global_ids, False)


async def retrieve_for_decision(query: str, decision: KnowledgeRetrievalDecision) -> list[dict[str, Any]]:
    """并行检索决策范围内的知识库并保留来源。"""
    if not decision.kb_ids:
        return []
    from yuxi.knowledge.runtime import knowledge_base

    clean_query = _MENTION_RE.sub("", query).strip()
    results = await asyncio.gather(
        *(knowledge_base.retrieve(kb_id, clean_query) for kb_id in decision.kb_ids),
        return_exceptions=True,
    )
    chunks: list[dict[str, Any]] = []
    for kb_id, result in zip(decision.kb_ids, results, strict=True):
        if isinstance(result, Exception):
            continue
        for item in result.get("results", []) if isinstance(result, dict) else []:
            if isinstance(item, dict):
                chunks.append({**item, "kb_id": item.get("kb_id") or kb_id})
    return chunks


def format_retrieval_context(chunks: list[dict[str, Any]]) -> str:
    """把检索结果转换为模型可读且可追溯的上下文。"""
    if not chunks:
        return "已搜索默认知识库，但没有找到相关内容。请不要编造知识库来源。"
    lines = ["以下内容来自本次允许检索的知识库，仅可据此回答相关事实："]
    for index, item in enumerate(chunks, 1):
        content = item.get("content") or item.get("text") or item.get("chunk") or ""
        lines.append(f"[{index}] kb_id={item.get('kb_id')} file_id={item.get('file_id')}\n{content}")
    return "\n\n".join(lines)
