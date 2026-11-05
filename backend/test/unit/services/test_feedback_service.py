"""新版反馈仅归属当前用户与明确 Turn 最终 Run，不读取旧 MessageFeedback 表。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuxi.modules.agents.scope import ActorScope
from yuxi.modules.agents.services import feedback as service
from yuxi.shared.errors import ResourceNotFoundError, StateConflictError


@pytest.fixture
def target(monkeypatch):
    """固定拥有者与结果事实，负例只改变待验证的归属或终态。"""
    scope = ActorScope(uid="user-1", app_id="app")
    turn = SimpleNamespace(id="turn-1", status="completed", result_run_id="run-1")
    run = SimpleNamespace(id="run-1", output_message_id=9)
    output = SimpleNamespace(id=9, run_id="run-1", turn_id="turn-1", session_record_id=7, role="assistant", content="完成的回答")
    db = SimpleNamespace(get=AsyncMock(return_value=output), commit=AsyncMock())
    monkeypatch.setattr(service, "require_thread", AsyncMock(return_value=SimpleNamespace(id=7)))
    monkeypatch.setattr(service.AgentTurnRepository, "get_for_scope", AsyncMock(return_value=turn))
    monkeypatch.setattr(service.AgentRunRepository, "get_for_scope", AsyncMock(return_value=run))
    return db, scope, turn, run, output


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["neighbor_result", "wrong_run", "wrong_turn", "wrong_session", "user_message", "empty_output"])
async def test_feedback_rejects_unbound_or_empty_result(target, invalid):
    db, scope, turn, run, output = target
    if invalid == "neighbor_result":
        turn.result_run_id = "run-neighbor"
    elif invalid == "wrong_run":
        output.run_id = "run-neighbor"
    elif invalid == "wrong_turn":
        output.turn_id = "turn-neighbor"
    elif invalid == "wrong_session":
        output.session_record_id = 8
    elif invalid == "user_message":
        output.role = "user"
    else:
        output.content = "   "
    with pytest.raises(StateConflictError):
        await service.require_feedback_target(db=db, scope=scope, thread_id="thread-1", turn_id=turn.id, run_id=run.id)
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_feedback_cannot_bypass_thread_owner(target, monkeypatch):
    db, scope, turn, run, _ = target
    monkeypatch.setattr(service, "require_thread", AsyncMock(side_effect=ResourceNotFoundError("Thread 不存在")))
    with pytest.raises(ResourceNotFoundError):
        await service.save_user_feedback(
            db=db, scope=scope, thread_id="foreign", turn_id=turn.id, run_id=run.id, rating="like", reason=None
        )
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_feedback_returns_only_after_commit_and_keeps_run_identity(target, monkeypatch):
    db, scope, turn, run, _ = target
    persisted = {"id": "feedback-1", "run_id": run.id, "source": "user", "rating": "dislike", "reason": "需要核对"}
    save = AsyncMock(return_value=SimpleNamespace(to_dict=lambda: persisted))
    monkeypatch.setattr(service.AgentFeedbackRepository, "save_user", save)
    result = await service.save_user_feedback(
        db=db, scope=scope, thread_id="thread-1", turn_id=turn.id, run_id=run.id, rating="dislike", reason=" 需要核对 "
    )
    assert result == persisted
    save.assert_awaited_once_with(run_id=run.id, rating="dislike", reason="需要核对")
    db.commit.assert_awaited_once()
    db.commit.side_effect = RuntimeError("commit failed")
    with pytest.raises(RuntimeError, match="commit failed"):
        await service.save_user_feedback(
            db=db, scope=scope, thread_id="thread-1", turn_id=turn.id, run_id=run.id, rating="dislike", reason="需要核对"
        )
