from types import SimpleNamespace

import pytest

from yuxi.services import knowledge_retrieval_policy as policy
from yuxi.knowledge.runtime import knowledge_base


def test_parse_knowledge_mentions_supports_quoted_and_unquoted_values():
    assert policy.parse_knowledge_mentions('@knowledge:制度库 @knowledge:"项目 资料"') == ("制度库", "项目 资料")


def test_classify_knowledge_intent_skips_obvious_non_knowledge_questions():
    assert policy.classify_knowledge_intent("帮我翻译这句话") == "NO_KB"
    assert policy.classify_knowledge_intent("公司制度里怎么规定") == "SEARCH_KB"


@pytest.mark.asyncio
async def test_decision_uses_mentions_only(monkeypatch):
    summaries = [
        SimpleNamespace(
            kb_id="kb-1", name="制度库", share_config={"version": 2, "read_scope": {"access_level": "global"}}
        ),
        SimpleNamespace(
            kb_id="kb-2", name="项目库", share_config={"version": 2, "read_scope": {"access_level": "global"}}
        ),
    ]
    monkeypatch.setattr(knowledge_base, "get_databases_by_uid", lambda uid: _return(summaries))
    decision = await policy.decide_knowledge_retrieval("@knowledge:制度库 怎么请假", SimpleNamespace(uid="u1"))
    assert decision.kb_ids == ("kb-1",)
    assert decision.mentioned is True


@pytest.mark.asyncio
async def test_decision_dynamically_selects_all_global_databases(monkeypatch):
    summaries = [
        SimpleNamespace(
            kb_id="kb-1", name="制度库", share_config={"version": 2, "read_scope": {"access_level": "global"}}
        ),
        SimpleNamespace(
            kb_id="kb-2", name="项目库", share_config={"version": 2, "read_scope": {"access_level": "global"}}
        ),
        SimpleNamespace(
            kb_id="kb-3", name="个人库", share_config={"version": 2, "read_scope": {"access_level": "user"}}
        ),
    ]
    monkeypatch.setattr(knowledge_base, "get_databases_by_uid", lambda uid: _return(summaries))
    decision = await policy.decide_knowledge_retrieval("公司制度怎么规定", SimpleNamespace(uid="u1"))
    assert decision.kb_ids == ("kb-1", "kb-2")


async def _return(value):
    return value


@pytest.mark.asyncio
async def test_non_knowledge_request_does_not_query_database(monkeypatch):
    """普通非检索请求不依赖知识库数据库可用性。"""

    async def unavailable(_uid):
        raise AssertionError("非检索请求不得查询知识库")

    monkeypatch.setattr(knowledge_base, "get_databases_by_uid", unavailable)
    decision = await policy.decide_knowledge_retrieval("你好", SimpleNamespace(uid="u1"))
    assert decision.intent == "NO_KB"
    assert decision.kb_ids == ()


@pytest.mark.asyncio
async def test_retrieval_keeps_article_sources_and_allowed_kb_names(monkeypatch):
    """实际策略输出同时保留可读库范围与文章级来源。"""

    async def retrieve(kb_id, query):
        assert (kb_id, query) == ("kb-1", "制度")
        return {"results": [{"file_id": "file-1", "content": "正文", "metadata": {"source": "制度.md"}}]}

    async def get_file_info(kb_id, file_id):
        assert (kb_id, file_id) == ("kb-1", "file-1")
        return {"content": "title  请假制度\nauthor  示例作者\n"}

    monkeypatch.setattr(knowledge_base, "retrieve", retrieve)
    monkeypatch.setattr(knowledge_base, "get_file_info", get_file_info)
    decision = policy.KnowledgeRetrievalDecision("SEARCH_KB", ("kb-1",), True, (("kb-1", "制度库"),))
    result = await policy.retrieve_for_decision("@knowledge:制度库 制度", decision)
    assert result[0]["kb_id"] == "kb-1"
    assert result[0]["metadata"]["source_ref"] == {
        "source_type": "knowledge_base",
        "title": "请假制度",
        "author": "示例作者",
        "kb_id": "kb-1",
        "kb_name": "制度库",
    }


# ==================== 三态意图：默认不再检索 ====================


def test_classify_knowledge_intent_returns_unknown_when_no_signal():
    """两边词表都未命中时返回 UNKNOWN，而不是像旧策略那样默认检索。"""
    assert policy.classify_knowledge_intent("minimax-pdf 是什么") == policy.INTENT_UNKNOWN
    assert policy.classify_knowledge_intent("上个月各产品线的销售明细") == policy.INTENT_UNKNOWN


def test_classify_knowledge_intent_treats_skill_inquiry_as_non_knowledge():
    """询问某个技能/工具本身是什么，属能力询问，必须确定性不检索。

    回归：带 @skill 的提及会让模型把产品名误判为内部系统而选择检索，且结果抖动。
    """
    cases = (
        "@skill:mysql-reporter 这是什么技能",
        "mysql reporter 这是什么技能",
        "mysql-reporter 是干嘛的",
        "pptx-manipulation 这是什么技能",
        "@skill:plan-a 怎么用",
        "html-preview 有什么功能",
    )
    for query in cases:
        assert policy.classify_knowledge_intent(query) == policy.INTENT_NO_SEARCH, query


def test_strip_mention_syntax_removes_skill_agent_and_knowledge_tokens():
    """意图判定前剥离提及语法，避免产品名干扰判断。"""
    assert policy.strip_mention_syntax("@skill:mysql-reporter 这是什么技能").strip() == "这是什么技能"
    assert policy.strip_mention_syntax('@agent:pm @knowledge:"制度库" 里怎么规定的') == "里怎么规定的"
    assert policy.strip_mention_syntax("@skill:x") == ""


def test_classify_knowledge_intent_prioritizes_internal_signals():
    """内部资料词优先于创作类词，避免显式查资料的问题被误判为不检索。"""
    assert policy.classify_knowledge_intent("公司制度怎么翻译") == policy.INTENT_SEARCH
    assert policy.classify_knowledge_intent("帮我在这份文档里翻译一段") == policy.INTENT_SEARCH


def test_classify_knowledge_intent_keeps_internal_priority_over_skill_inquiry():
    """@技能 + 需要内部资料的场景不得被能力询问规则误伤。"""
    assert policy.classify_knowledge_intent("@skill:material-matcher 按公司申报要求匹配资料") == policy.INTENT_SEARCH
    assert policy.classify_knowledge_intent("@skill:mysql-reporter 帮我查一下内部文档里的销售明细") == policy.INTENT_SEARCH


def test_classify_knowledge_intent_skips_capability_and_chitchat():
    """能力询问与闲聊不需要检索。"""
    assert policy.classify_knowledge_intent("你会干什么") == policy.INTENT_NO_SEARCH
    assert policy.classify_knowledge_intent("你能帮我翻译吗") == policy.INTENT_NO_SEARCH


@pytest.mark.asyncio
async def test_resolve_knowledge_intent_without_model_skips_retrieval():
    """规则判不出且没有可用模型时保守跳过检索。"""
    assert await policy.resolve_knowledge_intent("minimax-pdf 是什么", None) == policy.INTENT_NO_SEARCH
    assert await policy.resolve_knowledge_intent("minimax-pdf 是什么") == policy.INTENT_NO_SEARCH


@pytest.mark.asyncio
async def test_resolve_knowledge_intent_uses_model_only_for_unknown(monkeypatch):
    """规则能判定时不调模型；只有 UNKNOWN 才用低温度模型兜底。"""
    calls = []

    class _FakeModel:
        async def call(self, messages, stream=False):
            calls.append(messages)
            return SimpleNamespace(content='```json\n{"intent":"SEARCH"}\n```')

    monkeypatch.setattr("yuxi.models.chat.select_model", lambda *args, **kwargs: _FakeModel())
    assert await policy.resolve_knowledge_intent("minimax-pdf 是什么", "fake-model") == policy.INTENT_SEARCH
    assert len(calls) == 1
    # 规则已命中的问题不应再触发模型调用
    assert await policy.resolve_knowledge_intent("你好", "fake-model") == policy.INTENT_NO_SEARCH
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_resolve_knowledge_intent_falls_back_when_model_unavailable(monkeypatch):
    """模型选择或调用失败时按不检索处理，不再回退到“默认检索”。"""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("yuxi.models.chat.select_model", _boom)
    assert await policy.resolve_knowledge_intent("minimax-pdf 是什么", "fake-model") == policy.INTENT_NO_SEARCH


@pytest.mark.asyncio
async def test_ambiguous_question_without_model_does_not_query_database(monkeypatch):
    """规则判不出且无模型时既不检索也不查库。"""

    async def unavailable(_uid):
        raise AssertionError("非检索请求不得查询知识库")

    monkeypatch.setattr(knowledge_base, "get_databases_by_uid", unavailable)
    decision = await policy.decide_knowledge_retrieval("minimax-pdf 是什么", SimpleNamespace(uid="u1"))
    assert decision.intent == "NO_KB"
    assert decision.kb_ids == ()


@pytest.mark.asyncio
async def test_skill_inquiry_question_does_not_query_database(monkeypatch):
    """回归：@技能 + “这是什么技能” 不得触发知识库检索，也不应查询知识库或调用模型。"""

    async def unavailable(_uid):
        raise AssertionError("技能认知类提问不得查询知识库")

    model_calls: list[str] = []

    def _record(spec, **_kwargs):
        model_calls.append(spec)
        raise AssertionError("技能认知类提问不应调用意图模型")

    monkeypatch.setattr(knowledge_base, "get_databases_by_uid", unavailable)
    monkeypatch.setattr("yuxi.models.chat.select_model", _record)
    for query in ("@skill:mysql-reporter 这是什么技能", "mysql reporter 这是什么技能"):
        decision = await policy.decide_knowledge_retrieval(
            query, SimpleNamespace(uid="u1"), model_spec="fake-model"
        )
        assert decision.intent == "NO_KB", query
        assert decision.kb_ids == (), query
    assert model_calls == []
