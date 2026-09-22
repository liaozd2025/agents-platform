"""在工具结果裁切前保存引用原文，随 checkpoint 交接到最终回复。"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any
from urllib.parse import urlsplit

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.types import Command, Overwrite

# 本轮没有任何可信来源时向模型显式声明，避免它照协议自造 <cite> 身份。
NO_SOURCE_PROMPT = (
    "本次没有取得任何可引用的来源，禁止输出 <cite> 标签，也不要编造来源身份；"
    "直接回答即可。"
)
_CITE_TAG_RE = re.compile(r"<cite\b[^>]*>\s*\d*\s*</cite\s*>", re.IGNORECASE | re.DOTALL)
_CITE_SOURCE_ATTR_RE = re.compile(r"\bsource\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)


def _cite_source(tag: str) -> str:
    """读取单个 <cite> 标签声明的来源身份，缺失时返回空串。"""
    attr = _CITE_SOURCE_ATTR_RE.search(tag)
    return ((attr.group(1) or attr.group(2)) if attr else "").strip()


def merge_citation_sources(existing: list[dict] | None, incoming: list[dict] | None) -> list[dict]:
    """按来源身份合并原文窗口，兼容同一轮并行工具更新。"""
    merged: dict[str, dict] = {}
    for item in [*(existing or []), *(incoming or [])]:
        source = item["source"]
        previous = merged.get(source)
        if previous is None:
            merged[source] = {**item, "excerpts": list(item.get("excerpts") or [])}
            continue
        if previous.get("title") == previous.get("file_id") and item.get("title"):
            previous["title"] = item["title"]
        for excerpt in item.get("excerpts") or []:
            if excerpt not in previous["excerpts"]:
                previous["excerpts"].append(excerpt)
    return list(merged.values())


def knowledge_citation_sources(items: list[dict], *, kb_id: str = "") -> list[dict]:
    """从已授权检索或文档工具返回的真实窗口构造文件引用。"""
    sources = []
    for item in items:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") or {}
        ref = metadata.get("source_ref") or {}
        database_id = item.get("kb_id") or ref.get("kb_id") or kb_id
        file_id = item.get("file_id") or metadata.get("file_id")
        if not database_id or not file_id:
            continue
        source = {
            "source": f"kb://{database_id}/{file_id}",
            "source_type": "file",
            "title": ref.get("title") or metadata.get("source") or item.get("title") or str(file_id),
            "kb_id": str(database_id),
            "file_id": str(file_id),
            "excerpts": [],
        }
        if _safe_url(ref.get("url")):
            source["url"] = ref["url"]
        for window in item.get("windows", [item]):
            text = window.get("content") or window.get("text") or window.get("chunk")
            if not isinstance(text, str) or not text:
                continue
            excerpt = {"text": text}
            for key in ("start_line", "end_line"):
                if isinstance(window.get(key), int):
                    excerpt[key] = window[key]
            if chunk_id := window.get("chunk_id") or window.get("id"):
                excerpt["chunk_id"] = str(chunk_id)
            source["excerpts"].append(excerpt)
        sources.append(source)
    return merge_citation_sources([], sources)


def _safe_url(value: object) -> bool:
    """仅接受可以直接打开的绝对网页链接。"""
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        return False


def bound_citation_identities(sources: list[dict] | None) -> set[str]:
    """收集本轮可信来源的可用身份（稳定 source 与可直接打开的 url）。"""
    identities: set[str] = set()
    for item in sources or []:
        if not isinstance(item, dict):
            continue
        for key in ("source", "url"):
            value = item.get(key)
            if isinstance(value, str) and value:
                identities.add(value)
    return identities


def strip_unbound_citations(content: str, sources: list[dict] | None) -> str:
    """剥离模型自造身份的 <cite> 标签，只保留已绑定来源或可直接打开的链接。

    模型在没有任何来源时仍可能照引用协议编出一个 ``file:///…`` 身份，前端会
    把它渲染成没有对应原文的徽标。这里以本轮采集到的可信来源为准做确定性兜底。
    """
    if not isinstance(content, str) or "<cite" not in content.lower():
        return content
    identities = bound_citation_identities(sources)

    def _keep(match: re.Match[str]) -> str:
        source = _cite_source(match.group(0))
        return match.group(0) if source and (source in identities or _safe_url(source)) else ""

    return _CITE_TAG_RE.sub(_keep, content)


def collapse_duplicate_citations(content: str) -> str:
    """折叠紧邻且指向同一来源的重复 <cite> 标签。

    模型可能在结论同一处为同一份原文连续贴上多个引用标签（各自带自己的局部
    编号），前端按来源归一化编号后就会渲染成并排的相同徽标。这里只折叠中间
    仅有空白的相邻重复项；同一来源在正文不同位置被再次引用属于正常复引。
    """
    if not isinstance(content, str) or "<cite" not in content.lower():
        return content
    matches = list(_CITE_TAG_RE.finditer(content))
    if len(matches) < 2:
        return content

    dropped: list[tuple[int, int]] = []
    previous = matches[0]
    for match in matches[1:]:
        same_source = _cite_source(previous.group(0)) == _cite_source(match.group(0))
        if same_source and not content[previous.end() : match.start()].strip():
            dropped.append((previous.end(), match.end()))
            previous = match
            continue
        previous = match
    if not dropped:
        return content
    parts = []
    cursor = 0
    for start, end in dropped:
        parts.append(content[cursor:start])
        cursor = end
    parts.append(content[cursor:])
    return "".join(parts)


def _tool_sources(name: str, message: ToolMessage) -> list[dict]:
    """只解析已知工具协议，不把模型自述或搜索摘要当作原文。"""
    if message.status == "error":
        return []
    if name in {"task", "subagent_status", "subagent_await"}:
        return (message.artifact or {}).get("citation_sources", [])
    if name not in {"query_kb", "open_kb_document", "find_kb_document", "web_search"}:
        return []
    try:
        data = json.loads(message.content) if isinstance(message.content, str) else message.artifact
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    if name != "web_search":
        items = data.get("results", []) if name == "query_kb" else [data]
        return knowledge_citation_sources(items, kb_id=data.get("kb_id", ""))
    sources = []
    for item in data.get("results") or []:
        if not isinstance(item, dict) or not _safe_url(item.get("url")):
            continue
        raw_content = item.get("raw_content")
        sources.append(
            {
                "source": item["url"],
                "source_type": "url",
                "title": item.get("title") or item["url"],
                "url": item["url"],
                "excerpts": [{"text": raw_content}] if isinstance(raw_content, str) and raw_content else [],
            }
        )
    return merge_citation_sources([], sources)


class CitationState(AgentState):
    """引用证据独立于可被压缩的消息列表保存。"""

    citation_sources: Annotated[list[dict], merge_citation_sources]


class CitationMiddleware(AgentMiddleware[CitationState]):
    """捕获可信工具原文，并向模型提供稳定来源身份。"""

    state_schema = CitationState

    def before_agent(self, state, runtime):
        """新输入仅继承本次预检索；checkpoint resume 不重新进入此节点。"""
        sources = []
        for message in reversed(state.get("messages") or []):
            if isinstance(message, HumanMessage):
                sources = message.additional_kwargs.get("citation_sources") or []
                break
        return {"citation_sources": Overwrite(sources)}

    async def abefore_agent(self, state, runtime):
        """异步入口复用引用初始化。"""
        return self.before_agent(state, runtime)

    def _with_sources(self, request):
        """压缩或工具裁切后仍向模型提供本轮可用的来源身份。"""
        sources = request.state.get("citation_sources") or []
        if not sources:
            return request.override(system_message=append_to_system_message(request.system_message, NO_SOURCE_PROMPT))
        identities = [{key: source[key] for key in ("source", "source_type", "title")} for source in sources]
        prompt = "本次已取得的引用来源（仅为实际采用的结论引用）：\n" + json.dumps(identities, ensure_ascii=False)
        return request.override(system_message=append_to_system_message(request.system_message, prompt))

    def _clean(self, content: str, sources: list[dict] | None) -> str:
        """先剥离未绑定来源，再折叠相邻重复，避免删除后残留紧邻的相同徽标。"""
        return collapse_duplicate_citations(strip_unbound_citations(content, sources))

    def _drop_unbound_citations(self, response, sources):
        """回答落库前剥离未绑定来源的引用标签，避免前端渲染幽灵徽标。"""
        content = getattr(response, "content", None)
        if isinstance(content, str):
            cleaned = self._clean(content, sources)
            return response if cleaned == content else response.model_copy(update={"content": cleaned})
        if isinstance(content, list):
            blocks: list[Any] = []
            changed = False
            for block in content:
                text = block.get("text") if isinstance(block, dict) else None
                if isinstance(text, str):
                    cleaned = self._clean(text, sources)
                    if cleaned != text:
                        block = {**block, "text": cleaned}
                        changed = True
                blocks.append(block)
            return response.model_copy(update={"content": blocks}) if changed else response
        return response

    def wrap_model_call(self, request, handler):
        """同步模型调用附带来源身份，并回收未绑定来源的引用标签。"""
        response = handler(self._with_sources(request))
        return self._drop_unbound_citations(response, request.state.get("citation_sources") or [])

    async def awrap_model_call(self, request, handler):
        """异步模型调用附带来源身份，并回收未绑定来源的引用标签。"""
        response = await handler(self._with_sources(request))
        return self._drop_unbound_citations(response, request.state.get("citation_sources") or [])

    def wrap_tool_call(self, request, handler):
        """在外层 filesystem 裁切之前捕获原始工具响应。"""
        return self._capture(request.tool_call["name"], handler(request))

    async def awrap_tool_call(self, request, handler):
        """异步工具调用保留原始响应证据。"""
        return self._capture(request.tool_call["name"], await handler(request))

    def _capture(self, name, result):
        """用 Command 同时传递工具消息与可持久化的证据。"""
        update = dict(result.update) if isinstance(result, Command) and isinstance(result.update, dict) else {}
        messages = update.get("messages", []) if isinstance(result, Command) else [result]
        sources = []
        for message in messages:
            if not isinstance(message, ToolMessage):
                continue
            captured = _tool_sources(name, message)
            if captured:
                message.artifact = {**(message.artifact or {}), "citation_sources": captured}
                sources = merge_citation_sources(sources, captured)
        if not sources:
            return result
        update.update(messages=messages, citation_sources=sources)
        if isinstance(result, Command):
            from dataclasses import replace

            return replace(result, update=update)
        return Command(update=update)
