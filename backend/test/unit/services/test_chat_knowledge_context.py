"""知识库选库上下文的可信来源、事务顺序和失败审计。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import HumanMessage

from yuxi.services import chat_service as svc


def routing_fixture(monkeypatch, *, scope=None, selected=(), relation="same", error=None):
    """在服务边界提供一个可回读的当前 Run 和持久化记录。"""
    run = SimpleNamespace(conversation_thread_id="thread", input_message_id=17, run_type="chat", input_payload={})
    repo = SimpleNamespace(get_run_for_user=AsyncMock(return_value=run), set_knowledge_retrieval=AsyncMock())
    monkeypatch.setattr(svc, "AgentRunRepository", lambda db: repo)
    audit = {"assessments": [{"kb_id": "A", "missing_description": True}]}
    decision = SimpleNamespace(
        task_scope=scope,
        kb_ids=selected,
        allowed_kb_ids=("A",),
        task_relation=relation,
        clarification="需要哪一类内部资料？" if not selected else None,
        to_metadata=lambda: dict(audit),
    )
    choose = AsyncMock(return_value=decision, side_effect=error)
    retrieve = AsyncMock(return_value=[{"kb_id": "A", "content": "实际内容"}])
    monkeypatch.setattr(svc, "decide_knowledge_retrieval", choose)
    monkeypatch.setattr(svc, "retrieve_for_decision", retrieve)
    conv = SimpleNamespace(id=1, extra_metadata={"knowledge_task_scope": ["old"]})
    conv_repo = SimpleNamespace(
        knowledge_routing_history=AsyncMock(return_value=[{"role": "assistant", "content": "已有证据"}]),
        set_knowledge_task_scope=AsyncMock(),
    )
    args = dict(
        query="当前问题",
        human_message=HumanMessage(content="当前问题"),
        current_user=SimpleNamespace(uid="u"),
        conversation=conv,
        conv_repo=conv_repo,
        input_context={"model": "override:model", "knowledges": ["A"], "knowledge_task_scope": ["forged"]},
        meta={"run_id": "run", "thread_id": "thread", "worker_id": "worker"},
        db=SimpleNamespace(commit=AsyncMock()),
    )
    return args, run, repo, choose, retrieve


async def test_description_selection_uses_overridden_model_and_persists_scope_before_execution(monkeypatch):
    args, run, repo, choose, retrieve = routing_fixture(monkeypatch, scope=["A"], selected=("A",))
    message, chunks = await svc._prepare_knowledge_context(**args)
    assert choose.await_args.kwargs == {
        "model": "override:model",
        "history": [{"role": "assistant", "content": "已有证据"}],
        "enabled_knowledges": ["A"],
        "previous_scope": ["old"],
        "inherited_scope": None,
        "counters": {"directory_reads": 0, "selection_calls": 0, "content_queries": 0},
    }
    assert args["input_context"]["knowledge_task_scope"] == ["A"]
    assert args["input_context"]["knowledge_selected_kb_ids"] == ["A"]
    payload = repo.set_knowledge_retrieval.await_args.kwargs["payload"]
    assert payload["knowledge_allowed_kb_ids"] == ["A"]
    assert payload["knowledge_retrieval"]["result_count"] == 1
    assert args["db"].commit.await_count == 1
    assert "实际内容" in message.content and "提示管理员补充描述" in message.content
    assert retrieve.await_args.kwargs["user"] is args["current_user"]
    assert chunks == [{"kb_id": "A", "content": "实际内容"}]


async def test_new_task_resets_old_scope_and_uncertainty_does_not_query(monkeypatch):
    args, run, repo, choose, retrieve = routing_fixture(monkeypatch, relation="new")
    message, chunks = await svc._prepare_knowledge_context(**args)
    assert args["input_context"]["knowledge_task_scope"] is None
    args["conv_repo"].set_knowledge_task_scope.assert_awaited_once_with(args["conversation"], None)
    assert "简短追问" in message.content and "旧任务" in message.content
    assert args["meta"]["knowledge_retrieval"]["status"] == "clarify"
    retrieve.assert_not_awaited()
    assert chunks == []


async def test_child_scope_comes_from_current_run_not_meta_or_config(monkeypatch):
    args, run, repo, choose, retrieve = routing_fixture(monkeypatch)
    run.run_type = "subagent"
    run.input_payload = {"runtime": {"knowledge_task_scope": ["parent-allowed"]}}
    args["meta"]["knowledge_task_scope"] = ["forged-meta"]
    await svc._prepare_knowledge_context(**args)
    assert choose.await_args.kwargs["inherited_scope"] == ["parent-allowed"]


async def test_selection_failure_persists_error_and_never_becomes_empty_success(monkeypatch):
    error = ValueError("invalid response")
    error.code = "knowledge_selection_invalid"
    args, run, repo, choose, retrieve = routing_fixture(monkeypatch, error=error)
    with pytest.raises(ValueError, match="invalid response"):
        await svc._prepare_knowledge_context(**args)
    payload = repo.set_knowledge_retrieval.await_args.kwargs["payload"]
    assert payload["knowledge_retrieval"]["status"] == "error"
    assert payload["knowledge_retrieval"]["error_code"] == "knowledge_selection_invalid"
    assert payload["knowledge_allowed_kb_ids"] == []
    args["conv_repo"].set_knowledge_task_scope.assert_not_awaited()
    retrieve.assert_not_awaited()
    args["db"].commit.assert_awaited_once()


async def test_resume_restores_only_current_run_persisted_scope(monkeypatch):
    args, run, repo, choose, retrieve = routing_fixture(monkeypatch)
    run.input_payload = {
        "knowledge_task_scope": ["A"],
        "knowledge_selected_kb_ids": [],
        "knowledge_allowed_kb_ids": ["A"],
        "knowledge_retrieval": {"status": "skip"},
    }
    await svc._restore_knowledge_context(
        input_context=args["input_context"], meta=args["meta"], uid="u", thread_id="thread", db=args["db"]
    )
    assert args["input_context"]["knowledge_task_scope"] == ["A"]
    assert args["input_context"]["knowledge_selected_kb_ids"] == []
    assert args["meta"]["knowledge_retrieval"] == {"status": "skip"}
    repo.get_run_for_user.assert_awaited_once_with("run", "u")
    choose.assert_not_awaited()


@pytest.mark.parametrize("scope", [None, "A", [1]])
async def test_malformed_persisted_child_scope_cannot_become_unrestricted(monkeypatch, scope):
    """持久输入也要校验，特别不能把子任务 null 范围解释成不受限制。"""
    args, run, repo, choose, retrieve = routing_fixture(monkeypatch)
    run.run_type = "subagent"
    run.input_payload = {"runtime": {"knowledge_task_scope": scope}}
    with pytest.raises(ValueError, match="服务端固化的列表"):
        await svc._prepare_knowledge_context(**args)
    choose.assert_not_awaited()
    retrieve.assert_not_awaited()


async def test_retrieval_failure_preserves_successfully_selected_task_scope(monkeypatch):
    """内容后端失败不能撤销本轮已经确认的范围。"""
    args, run, repo, choose, retrieve = routing_fixture(monkeypatch, scope=["A"], selected=("A",))
    retrieve.side_effect = RuntimeError("retrieval unavailable")
    with pytest.raises(RuntimeError, match="retrieval unavailable"):
        await svc._prepare_knowledge_context(**args)
    args["conv_repo"].set_knowledge_task_scope.assert_awaited_once_with(args["conversation"], ["A"])
    assert repo.set_knowledge_retrieval.await_args.kwargs["payload"]["knowledge_retrieval"]["status"] == "error"
    args["db"].commit.assert_awaited_once()
