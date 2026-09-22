"""聊天请求的知识库检索范围与轻量意图策略。"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r'@knowledge:(?:"((?:\\.|[^"\\])*)"|(\S+))')
# 任意提及语法（@skill:xxx / @agent:xxx / @knowledge:xxx），仅用于意图判定前剥离。
# 不剥离会让模型把“mysql reporter”这类产品名误当成内部系统，从而误判需要检索知识库。
_ANY_MENTION_RE = re.compile(r'@[A-Za-z_][\w-]*:(?:"(?:\\.|[^"\\])*"|\S+)')
# 内部资料信号：命中即检索，优先级高于非检索词（避免“公司制度怎么翻译”被创作类词覆盖）
_INTERNAL_KB_PATTERNS = (
    "知识库",
    "内部",
    "公司",
    "部门",
    "我们",
    "制度",
    "规定",
    "流程",
    "规范",
    "标准",
    "文档",
    "文件",
    "资料",
    "报告",
    "纪要",
    "项目",
)
# 明确不需要检索的问题类型
_NON_KB_PATTERNS = (
    "你好",
    "您好",
    "谢谢",
    "晚安",
    "早上好",
    "你是谁",
    "介绍一下自己",
    "你会干什么",
    "几点",
    "天气",
    "翻译",
    "润色",
    "改写",
    "改成",
    "总结一下",
    "起个名字",
    "生成图片",
    "生成一张图",
    "画图",
    "写代码",
    "代码格式",
    "格式化代码",
    "计算",
    "等于多少",
)
# 能力询问（如“你能查公司制度吗”），归为不检索
_CAPABILITY_PATTERN = re.compile(r"(?:你能|能否|可以|支持|会不会|能不能).{0,30}(?:吗|么|？|\?)")
# 技能/工具认知类询问（如“mysql-reporter 这是什么技能”“XX 怎么用”）。
# 这类问题问的是“某个工具/技能本身是什么”，与内部资料无关，必须确定性判为不检索，
# 不能交给模型兜底——实测模型看到陌生产品名会误判为“疑似内部系统”而选择检索，且结果会抖动。
_SKILL_INQUIRY_PATTERN = re.compile(
    r"(?:什么技能|什么工具|什么能力|是什么东西|是干嘛|干嘛的|干什么用|做什么用|什么用的"
    r"|怎么用|如何使用|怎么使用|能做什么|主要功能|有什么功能|有哪些功能|有什么特性|有什么作用|有什么用)"
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

INTENT_SEARCH = "SEARCH_KB"
INTENT_NO_SEARCH = "NO_KB"
INTENT_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class KnowledgeRetrievalDecision:
    """一次请求最终使用的知识库检索决策。"""

    intent: str
    kb_ids: tuple[str, ...]
    mentioned: bool
    kb_names: tuple[tuple[str, str], ...] = ()


def parse_knowledge_mentions(query: str) -> tuple[str, ...]:
    """解析消息中的 @knowledge token，返回去重后的值。"""
    values: list[str] = []
    for match in _MENTION_RE.finditer(str(query or "")):
        value = match.group(1) if match.group(1) is not None else match.group(2)
        value = value.replace('\\"', '"').replace("\\\\", "\\")
        if value and value not in values:
            values.append(value)
    return tuple(values)


def strip_mention_syntax(query: str) -> str:
    """剥离 @skill:@agent:@knowledge: 等提及语法，只留用户真实问题。

    仅供意图判定使用；@knowledge 提及本身仍由 ``parse_knowledge_mentions`` 从原文解析。
    """
    return _ANY_MENTION_RE.sub(" ", str(query or "")).strip()


def classify_knowledge_intent(query: str) -> str:
    """用双向规则判断知识库意图；两边都未命中时返回 UNKNOWN。"""
    text = strip_mention_syntax(query)
    if not text:
        return INTENT_NO_SEARCH
    # 内部资料信号优先，避免“公司制度怎么翻译”被创作类词覆盖。
    # 该优先级同时保证「@技能 + 需要查内部资料」的场景不会被能力询问规则误伤。
    if any(pattern in text for pattern in _INTERNAL_KB_PATTERNS) or any(pattern in text for pattern in _KB_PATTERNS):
        return INTENT_SEARCH
    if (
        any(pattern in text for pattern in _NON_KB_PATTERNS)
        or _CAPABILITY_PATTERN.search(text)
        or _SKILL_INQUIRY_PATTERN.search(text)
    ):
        return INTENT_NO_SEARCH
    return INTENT_UNKNOWN


async def resolve_knowledge_intent(query: str, model_spec: str | None = None) -> str:
    """先用规则分类；无法确定时用低温度模型判断是否必须查内部资料。"""
    rule_result = classify_knowledge_intent(query)
    if rule_result != INTENT_UNKNOWN:
        return rule_result
    # 没有可用模型时保守跳过检索，避免像旧策略那样对所有问题都发起检索。
    if not model_spec:
        return INTENT_NO_SEARCH
    try:
        from yuxi.models.chat import select_model

        model = select_model(model_spec, temperature=0)
        response = await model.call(
            [
                {
                    "role": "system",
                    "content": (
                        "判断用户问题是否必须依赖公司内部资料才能准确回答。"
                        '只返回JSON，不要解释，格式为 {"intent":"SEARCH"} 或 {"intent":"NO_SEARCH"}。'
                        "内部制度、项目事实、公司文档、历史记录等选 SEARCH；"
                        "通用知识、闲聊、能力询问、创作任务，以及询问某个技能/工具/功能本身是什么或怎么用，选 NO_SEARCH。"
                    ),
                },
                {"role": "user", "content": strip_mention_syntax(query)},
            ],
            stream=False,
        )
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content).strip(), flags=re.IGNORECASE)
        payload = json.loads(raw)
        intent = str(payload.get("intent", "")).strip().upper() if isinstance(payload, dict) else ""
        return INTENT_SEARCH if intent == "SEARCH" else INTENT_NO_SEARCH
    except Exception:
        logger.warning("知识库意图模型判断失败，按不检索处理", exc_info=True)
        return INTENT_NO_SEARCH


async def decide_knowledge_retrieval(
    query: str, user: Any, *, model_spec: str | None = None
) -> KnowledgeRetrievalDecision:
    """解析 @ 范围或动态选择当前用户可读的全局知识库。"""
    mentions = parse_knowledge_mentions(query)
    # 显式 @ 知识库是用户给出的明确约束，直接检索；否则先判定意图，规则判不出再交由模型兜底。
    if not mentions and await resolve_knowledge_intent(query, model_spec) == INTENT_NO_SEARCH:
        return KnowledgeRetrievalDecision(INTENT_NO_SEARCH, (), False)

    from yuxi.knowledge.runtime import knowledge_base

    summaries = await knowledge_base.get_databases_by_uid(str(user.uid))
    visible = {str(summary.kb_id): summary for summary in summaries}
    if mentions:
        kb_ids = tuple(
            next((kb_id for kb_id, summary in visible.items() if kb_id == value or summary.name == value), value)
            for value in mentions
            if value in visible or any(summary.name == value for summary in visible.values())
        )
        return KnowledgeRetrievalDecision(
            "SEARCH_KB", kb_ids, True, tuple((kb_id, str(visible[kb_id].name)) for kb_id in kb_ids)
        )

    global_ids = tuple(
        kb_id
        for kb_id, summary in visible.items()
        if isinstance(summary.share_config, dict)
        and isinstance(summary.share_config.get("read_scope"), dict)
        and summary.share_config["read_scope"].get("access_level") == "global"
    )
    return KnowledgeRetrievalDecision(
        "SEARCH_KB", global_ids, False, tuple((kb_id, str(visible[kb_id].name)) for kb_id in global_ids)
    )


async def retrieve_for_decision(query: str, decision: KnowledgeRetrievalDecision) -> list[dict[str, Any]]:
    """并行检索决策范围内的知识库并保留来源。"""
    if not decision.kb_ids:
        return []
    from yuxi.knowledge.runtime import knowledge_base
    from yuxi.knowledge.source_references import attach_knowledge_source_references

    kb_names = dict(decision.kb_names)
    clean_query = _MENTION_RE.sub("", query).strip()
    results = await asyncio.gather(
        *(knowledge_base.retrieve(kb_id, clean_query) for kb_id in decision.kb_ids),
        return_exceptions=True,
    )
    chunks: list[dict[str, Any]] = []
    for kb_id, result in zip(decision.kb_ids, results, strict=True):
        if isinstance(result, Exception):
            continue
        result = await attach_knowledge_source_references(knowledge_base, kb_id, kb_names.get(kb_id, ""), result)
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
        metadata = item.get("metadata") or {}
        file_id = item.get("file_id") or metadata.get("file_id")
        source = f"kb://{item.get('kb_id')}/{file_id}" if file_id else "来源身份缺失，不可生成文件引用"
        lines.append(f"[{index}] source={source}\n{content}")
    return "\n\n".join(lines)
