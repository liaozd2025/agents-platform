import copy
import json
from types import SimpleNamespace

import pytest

from yuxi.knowledge.runtime import knowledge_base
from yuxi.services import knowledge_retrieval_policy as policy


def _result(statuses, **kwargs):
    """建立独立标注的模型协议响应。"""
    return {
        "task_relation": "continue",
        "explicit_scope": None,
        "requested_kb_ids": [],
        "assessments": [{"kb_id": kb_id, "status": status, "reason": "需要核对具体制度"} for kb_id, status in statuses],
        "clarification": None,
        **kwargs,
    }


@pytest.fixture
def routing(monkeypatch):
    """固定目录与模型协议，保留真实策略和范围校验。"""
    state = SimpleNamespace(
        summaries=[
            SimpleNamespace(kb_id="a", name="制度库", description="请假制度"),
            SimpleNamespace(kb_id="b", name="游戏项目库", description="HTML5 2048项目规范"),
            SimpleNamespace(kb_id="c", name="私人资料", description="   "),
        ],
        response=_result([("a", "skip"), ("b", "select"), ("c", "skip")]),
        calls=[],
        context_length=100_000,
    )

    async def directory(uid):
        assert uid == "u1"
        return state.summaries

    async def invoke(messages):
        payload = json.loads(messages[-1]["content"])
        state.calls.append(payload)
        if isinstance(state.response, Exception):
            raise state.response
        return state.response(payload) if callable(state.response) else copy.deepcopy(state.response)

    def load(model, **kwargs):
        assert model == "configured:model"
        return SimpleNamespace(with_structured_output=lambda schema: SimpleNamespace(ainvoke=invoke))

    monkeypatch.setattr(knowledge_base, "get_databases_by_uid", directory)
    monkeypatch.setattr(policy, "load_chat_model", load)
    monkeypatch.setattr(policy.model_cache, "get_model_info", lambda model: state)
    return state


async def _decide(query="制作2048小游戏", **kwargs):
    """调用当前策略入口。"""
    return await policy.decide_knowledge_retrieval(query, SimpleNamespace(uid="u1"), model="configured:model", **kwargs)


def test_parse_knowledge_mentions():
    assert policy.parse_knowledge_mentions('@knowledge:制度库 @knowledge:"项目 资料"') == ("制度库", "项目 资料")


@pytest.mark.asyncio
async def test_same_question_uses_descriptions_and_all_visible_kinds(routing):
    decision = await _decide()
    assert decision.kb_ids == ("b",)
    assert len(routing.calls) == 1
    assert len(routing.calls[0]["catalog"]) == 3
    assert routing.calls[0]["catalog"][2]["missing_description"] is True
    assert decision.assessments[2]["missing_description"] is True
    assert decision.allowed_kb_ids == ("a", "b", "c")
    assert decision.task_scope is None
    routing.summaries[1].description = "不包含游戏资料的日常随笔"
    routing.response = _result([("a", "skip"), ("b", "skip"), ("c", "skip")])
    decision = await _decide()
    assert decision.kb_ids == () and decision.intent == "NO_KB"
    assert decision.to_metadata()["assessments"][1]["status"] == "skip"


@pytest.mark.asyncio
async def test_multiple_libraries_and_missing_description_are_allowed(routing):
    routing.response = _result([("a", "select"), ("b", "skip"), ("c", "select")])
    assert (await _decide()).kb_ids == ("a", "c")


@pytest.mark.asyncio
async def test_uncertainty_asks_before_retrieval(routing):
    routing.response = _result([("a", "select"), ("b", "uncertain"), ("c", "skip")], clarification="使用哪个项目？")
    decision = await _decide()
    assert decision.intent == "CLARIFY" and decision.kb_ids == ()
    assert decision.clarification == "使用哪个项目？"


@pytest.mark.asyncio
async def test_scope_persists_until_new_task_and_inherited_scope_never_expands(routing):
    with pytest.raises(policy.KnowledgeSelectionError, match="scope_expansion_denied"):
        await _decide(previous_scope=["a"])
    routing.response["task_relation"] = "new"
    decision = await _decide(previous_scope=["a"])
    assert decision.task_scope is None and decision.kb_ids == ("b",)
    routing.response = _result([("a", "skip")], task_relation="new", explicit_scope=["b"])
    with pytest.raises(policy.KnowledgeSelectionError, match="invalid_selection"):
        await _decide(previous_scope=["a"], inherited_scope=["a"])
    assert [item["kb_id"] for item in routing.calls[-1]["catalog"]] == ["a"]


@pytest.mark.asyncio
async def test_explicit_scope_is_not_automatic_retrieval(routing):
    routing.response = _result([("a", "skip"), ("b", "skip"), ("c", "skip")], explicit_scope=["a"])
    decision = await _decide("只用制度库，把刚才的结果改成表格")
    assert decision.task_scope == ("a",) and decision.kb_ids == ()
    decision = await _decide("只用 @knowledge:制度库，把报告改排版")
    assert decision.task_scope == ("a",) and decision.kb_ids == ()
    routing.response["requested_kb_ids"] = ["a"]
    decision = await _decide("@knowledge:制度库 查请假制度")
    assert decision.kb_ids == ("a",)
    assert decision.assessments[0]["status"] == "select"


@pytest.mark.asyncio
async def test_agent_empty_scope_and_unavailable_mentions(routing):
    routing.response = _result([])
    decision = await _decide(enabled_knowledges=[])
    assert decision.kb_ids == decision.allowed_kb_ids == ()
    assert routing.calls == []
    routing.summaries = []
    decision = await _decide(previous_scope=["a"], inherited_scope=["a", "b"])
    assert decision.task_scope == ("a",)
    assert decision.kb_ids == () and routing.calls == []
    with pytest.raises(policy.KnowledgeSelectionError, match="explicit_scope_unavailable"):
        await _decide("@knowledge:制度库 查制度", enabled_knowledges=[])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["missing", "duplicate", "unknown", "bad_status", "extra", "blank_reason", "wrong_type"]
)
async def test_invalid_model_output_is_not_a_successful_empty_selection(routing, corruption):
    response = routing.response
    if corruption == "missing":
        response["assessments"].pop()
    elif corruption == "duplicate":
        response["assessments"].append(response["assessments"][0])
    elif corruption == "unknown":
        response["assessments"][0]["kb_id"] = "secret"
    elif corruption == "bad_status":
        response["assessments"][0]["status"] = "maybe"
    elif corruption == "extra":
        response["allow_all"] = True
    elif corruption == "blank_reason":
        response["assessments"][0]["reason"] = " "
    else:
        response["explicit_scope"] = "a"
    with pytest.raises(policy.KnowledgeSelectionError, match="invalid_selection"):
        await _decide()


@pytest.mark.asyncio
async def test_provider_failure_is_redacted(routing):
    routing.response = RuntimeError("credential=private-provider-token")
    counters = {"directory_reads": 0, "selection_calls": 0, "content_queries": 0}
    with pytest.raises(policy.KnowledgeSelectionError) as exc:
        await _decide(counters=counters)
    assert counters == {"directory_reads": 1, "selection_calls": 1, "content_queries": 0}
    assert exc.value.code == "selection_unavailable"
    assert "private-provider-token" not in str(exc.value)


@pytest.mark.asyncio
async def test_batches_cover_every_candidate_and_fix_task_decision(routing):
    routing.summaries = [SimpleNamespace(kb_id=str(i), name=f"库{i}", description="制度内容" * 100) for i in range(9)]
    routing.context_length = 5500
    routing.response = lambda payload: _result(
        [(item["kb_id"], "skip") for item in payload["catalog"]], **payload.get("fixed_task", {})
    )
    counters = {"directory_reads": 0, "selection_calls": 0, "content_queries": 0}
    decision = await _decide(counters=counters)
    assert counters == {"directory_reads": 1, "selection_calls": len(routing.calls), "content_queries": 0}
    assert len(routing.calls) > 1
    assert [item["kb_id"] for call in routing.calls for item in call["catalog"]] == [str(i) for i in range(9)]
    assert all(call["fixed_task"]["task_relation"] == "continue" for call in routing.calls[1:])
    assert len(decision.assessments) == 9
    routing.summaries[0].description *= 1000
    with pytest.raises(policy.KnowledgeSelectionError, match="selection_context_exceeded"):
        await _decide()


@pytest.mark.asyncio
async def test_retrieval_rechecks_permission_and_reports_errors(routing, monkeypatch):
    decision = await _decide()
    routing.summaries = []
    with pytest.raises(policy.KnowledgeRetrievalError, match="permission_denied"):
        await policy.retrieve_for_decision("游戏", decision, user=SimpleNamespace(uid="u1"))
    routing.summaries = [SimpleNamespace(kb_id="b")]

    async def broken(*args):
        raise RuntimeError("secret response")

    monkeypatch.setattr(knowledge_base, "retrieve", broken)
    with pytest.raises(policy.KnowledgeRetrievalError, match="retrieval_unavailable"):
        await policy.retrieve_for_decision("游戏", decision, user=SimpleNamespace(uid="u1"))


@pytest.mark.asyncio
async def test_retrieval_keeps_article_sources(routing, monkeypatch):
    routing.response = _result([("a", "select"), ("b", "skip"), ("c", "skip")])
    decision = await _decide()

    async def retrieve(kb_id, query):
        assert (kb_id, query) == ("a", "制度")
        return {"results": [{"file_id": "file-1", "content": "正文", "metadata": {"source": "制度.md"}}]}

    async def get_file_info(kb_id, file_id):
        return {"content": "title  请假制度\nauthor  示例作者\n"}

    monkeypatch.setattr(knowledge_base, "retrieve", retrieve)
    monkeypatch.setattr(knowledge_base, "get_file_info", get_file_info)
    result = await policy.retrieve_for_decision("@knowledge:制度库 制度", decision, user=SimpleNamespace(uid="u1"))
    assert result[0]["kb_id"] == "a"
    assert result[0]["metadata"]["source_ref"]["kb_name"] == "制度库"
    assert result[0]["metadata"]["source_ref"]["title"] == "请假制度"


@pytest.mark.asyncio
async def test_natural_language_query_overrides_skip_but_cannot_expand_task(routing):
    routing.response = _result(
        [("a", "skip"), ("b", "skip"), ("c", "skip")], explicit_scope=["a"], requested_kb_ids=["a"]
    )
    assert (await _decide("只用制度库，请查请假制度")).kb_ids == ("a",)
    with pytest.raises(policy.KnowledgeSelectionError, match="scope_expansion_denied"):
        await _decide(previous_scope=["b"])
    routing.response = _result([("a", "skip"), ("b", "skip"), ("c", "skip")], task_relation="uncertain")
    decision = await _decide(previous_scope=["a"])
    assert decision.intent == "CLARIFY" and decision.task_scope == ("a",)


@pytest.mark.asyncio
async def test_directory_failure_is_explicit(routing, monkeypatch):
    async def unavailable(uid):
        raise RuntimeError("private database error")

    monkeypatch.setattr(knowledge_base, "get_databases_by_uid", unavailable)
    with pytest.raises(policy.KnowledgeSelectionError, match="directory_unavailable"):
        await _decide()


@pytest.mark.asyncio
async def test_inconsistent_scope_across_batches_is_rejected(routing):
    routing.summaries = [SimpleNamespace(kb_id=str(i), name=f"库{i}", description="制度内容" * 100) for i in range(9)]
    routing.context_length = 5500
    routing.response = lambda payload: _result(
        [(item["kb_id"], "skip") for item in payload["catalog"]],
        task_relation="new" if "fixed_task" in payload else "continue",
    )
    with pytest.raises(policy.KnowledgeSelectionError, match="inconsistent_task_scope"):
        await _decide()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{}, {"results": "bad"}, {"results": [{"kb_id": "secret"}]}])
async def test_invalid_retrieval_output_is_not_empty_success(routing, monkeypatch, result):
    decision = await _decide()

    async def retrieve(*args):
        return result

    monkeypatch.setattr(knowledge_base, "retrieve", retrieve)
    with pytest.raises(policy.KnowledgeRetrievalError, match="invalid_retrieval_result"):
        await policy.retrieve_for_decision("游戏", decision, user=SimpleNamespace(uid="u1"))


@pytest.mark.asyncio
async def test_ambiguous_task_switch_preserves_scope_and_clarifies_cross_library_request(routing):
    """跨库意图未澄清前保留原任务范围，不把追问变成越界错误。"""
    routing.response = _result(
        [("a", "skip"), ("b", "select"), ("c", "skip")],
        task_relation="uncertain",
        explicit_scope=["b"],
        requested_kb_ids=["b"],
        clarification="这是继续原任务，还是切换到项目库的新任务？",
    )
    decision = await _decide("那就看看 @knowledge:游戏项目库", previous_scope=["a"])
    assert decision.intent == "CLARIFY" and decision.kb_ids == ()
    assert decision.task_scope == ("a",) and decision.allowed_kb_ids == ("a",)
    assert decision.clarification == "这是继续原任务，还是切换到项目库的新任务？"


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_scope", [None, ["a", "b"]])
async def test_uncertain_task_boundary_still_applies_explicit_narrowing(routing, previous_scope):
    """任务是否切换尚不确定时，用户明确缩小的范围仍必须立即生效。"""
    routing.response = _result(
        [("a", "skip"), ("b", "select"), ("c", "skip")],
        task_relation="uncertain",
        explicit_scope=["a"],
        clarification="这是继续原任务还是新任务？",
    )
    decision = await _decide("只用制度库", previous_scope=previous_scope)
    assert decision.intent == "CLARIFY" and decision.kb_ids == ()
    assert decision.task_scope == ("a",) and decision.allowed_kb_ids == ("a",)
