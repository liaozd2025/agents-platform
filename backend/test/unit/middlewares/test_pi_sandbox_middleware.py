from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from yuxi.agents.middlewares import pi_sandbox


@pytest.mark.asyncio
@pytest.mark.parametrize("stage_status", [None, "completed", "needs_input", "failed"])
async def test_pi_sandbox_delegates_complete_task_and_returns_project_artifacts(monkeypatch, tmp_path, stage_status):
    shared_root = tmp_path / "shared"
    skill_dir = shared_root / "dept-work-report"
    skill_dir.mkdir(parents=True)
    context = SimpleNamespace(
        uid="user-1",
        run_id="parent-run",
        thread_id="thread-parent",
        runtime_scope_id="thread-parent",
        workdir_path="/home/gem/user-data/projects/project-1",
        _effective_skill_slugs=["dept-work-report"],
        _runtime_skills={
            "dept-work-report": {
                "path": "/home/gem/skills/dept-work-report/SKILL.md",
            }
        },
    )
    calls = []

    @asynccontextmanager
    async def session_context():
        yield object()

    class Service:
        def __init__(self, _db):
            pass

        async def start(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(run=SimpleNamespace(id="child-run"), created=True)

    async def execute(run_id):
        calls.append({"executed": run_id})

    async def result(**_kwargs):
        return {
            "status": "completed",
            "output": "done",
            "thread_id": "pi-child-thread",
            "pi": {
                "stage_status": stage_status,
                "checks": ["原始数据共 80 行，总额 2400"],
                "unresolved_items": ["需确认退货是否计入销售额"] if stage_status == "needs_input" else [],
                "source_run_id": "original-edit-run",
                "output_subdir": "pi-runs/0123456789abcdef01234567",
                "artifact": {"files": [{"path": "report.sources.md"}]},
            },
        }

    monkeypatch.setattr(pi_sandbox.pg_manager, "get_async_session_context", session_context)
    monkeypatch.setattr(pi_sandbox, "PiSandboxRunService", Service)
    monkeypatch.setattr(pi_sandbox, "execute_pi_sandbox_run", execute)
    monkeypatch.setattr(pi_sandbox.agent_run_service, "load_agent_run_result", result)
    monkeypatch.setattr(pi_sandbox, "get_user_skills_root_dir", lambda _uid: shared_root)
    monkeypatch.setattr(pi_sandbox, "get_personal_skills_root_dir", lambda _uid: tmp_path / "personal")

    middleware = pi_sandbox.create_pi_sandbox_middleware(context)
    command = await middleware.tools[0].coroutine(
        description="读取周报并生成月报来源索引",
        runtime=SimpleNamespace(tool_call_id="call-1"),
        source_run_id="original-edit-run",
    )

    assert middleware.tools[0].name == "pi_sandbox"
    assert calls[0]["skill_sources"] == {"dept-work-report": skill_dir}
    assert calls[0]["skill_runtime_paths"] == {"dept-work-report": "/home/gem/skills/dept-work-report"}
    assert calls[1] == {"executed": "child-run"}
    if stage_status in {"needs_input", "failed"}:
        assert "artifacts" not in command.update
        assert "已登记的交付物" not in command.update["messages"][0].content
    else:
        assert command.update["artifacts"] == [
            "/home/gem/user-data/projects/project-1/outputs/pi-runs/0123456789abcdef01234567/report.sources.md"
        ]
    assert command.update["subagent_runs"] == [
        {
            "id": "call-1",
            "run_id": "child-run",
            "subagent_slug": "pi_sandbox",
            "subagent_name": "PI Agent",
            "child_thread_id": "pi-child-thread",
            "status": "completed",
            "events_url": "/api/agent/runs/child-run/events",
            "result_url": "/api/agent/runs/child-run/result",
        }
    ]
    assert "done" in command.update["messages"][0].content
    assert "child-run" in command.update["messages"][0].content
    assert "original-edit-run" in command.update["messages"][0].content
    assert "原始数据共 80 行，总额 2400" in command.update["messages"][0].content
    assert command.update["messages"][0].status == ("error" if stage_status == "failed" else "success")
    if stage_status is None:
        assert "未报告，需核对业务结果" in command.update["messages"][0].content
    if stage_status == "needs_input":
        assert "需确认退货是否计入销售额" in command.update["messages"][0].content
    assert calls[0]["source_run_id"] == "original-edit-run"

    monkeypatch.setattr(
        pi_sandbox.agent_run_service,
        "load_agent_run_result",
        AsyncMock(
            return_value={
                "status": "failed",
                "thread_id": "pi-child-thread",
                "error": {"message": "PI 续接来源无效"},
            }
        ),
    )
    failed = await middleware.tools[0].coroutine(
        description="修正原稿", runtime=SimpleNamespace(tool_call_id="call-2"), source_run_id="bad-source"
    )
    message = failed.update["messages"][0]
    assert message.status == "error"
    assert "PI 续接来源无效" in message.content
    assert "新上下文" not in message.content
