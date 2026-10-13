from types import SimpleNamespace

import pytest

from yuxi.agents.middlewares.citations import knowledge_citation_sources
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


@pytest.mark.asyncio
async def test_retrieval_removes_nul_from_content_and_citation_metadata(monkeypatch):
    async def retrieve(_kb_id, _query):
        return {"results": [{"file_id": "file-1", "content": "原文\x00片段", "metadata": {"source": "文档\x00.pdf"}}]}

    async def get_file_info(_kb_id, _file_id):
        return {"content": "title  说明书\x00正文\n"}

    monkeypatch.setattr(knowledge_base, "retrieve", retrieve)
    monkeypatch.setattr(knowledge_base, "get_file_info", get_file_info)
    decision = policy.KnowledgeRetrievalDecision("SEARCH_KB", ("kb-1",), True, (("kb-1", "制度库"),))

    result = await policy.retrieve_for_decision("问题", decision)
    citations = knowledge_citation_sources(result)

    assert result[0]["content"] == "原文片段"
    assert result[0]["metadata"]["source"] == "文档.pdf"
    assert result[0]["metadata"]["source_ref"]["title"] == "说明书正文"
    assert citations[0]["excerpts"] == [{"text": "原文片段"}]
    assert "\x00" not in policy.format_retrieval_context(result)
