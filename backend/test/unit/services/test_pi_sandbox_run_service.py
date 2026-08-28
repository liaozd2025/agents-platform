from types import SimpleNamespace

import pytest
from yuxi.services import pi_sandbox_run_service as svc
from yuxi.utils.hash_utils import hash_id


class Db:
    def __init__(self):
        self.commits = 0

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_start_inherits_parent_runtime_project_model_and_locked_skills(monkeypatch, tmp_path):
    db = Db()
    service = svc.PiSandboxRunService(db)
    parent = SimpleNamespace(
        id="parent-run",
        run_type="chat",
        agent_slug="assistant",
        conversation_thread_id="thread-parent",
        runtime_scope_id="runtime-parent",
        conversation_id=10,
        status="running",
        input_payload={"model_spec": "provider:model", "tool_approval_mode": "always_trust"},
    )
    parent_conversation = SimpleNamespace(id=10, uid="user-1", project_id="project-1")
    child = SimpleNamespace(id="child-run")
    captured = {}

    class RunRepo:
        async def lock_run_for_user(self, run_id, uid):
            assert (run_id, uid) == ("parent-run", "user-1")
            return parent

    class ConvRepo:
        async def get_conversation_by_id(self, conversation_id):
            assert conversation_id == 10
            return parent_conversation

        async def get_conversation_by_thread_id(self, thread_id):
            captured["child_thread_id"] = thread_id
            return None

        async def add_conversation(self, **kwargs):
            captured["conversation"] = kwargs
            return SimpleNamespace(id=20, status="active", **kwargs)

    async def resolve_binding(**kwargs):
        assert kwargs["conversation"] is parent_conversation
        return "projects/project-1", SimpleNamespace(id="project-1")

    async def prepare_scope(**kwargs):
        captured["scope"] = kwargs
        return SimpleNamespace(conversation=SimpleNamespace(id=20), existing_run=None)

    async def create_input(**_kwargs):
        return SimpleNamespace(id=30)

    async def persist(**kwargs):
        captured["persist"] = kwargs
        return child, True

    service.run_repo = RunRepo()
    service.conv_repo = ConvRepo()
    monkeypatch.setattr(svc, "resolve_conversation_workdir_binding", resolve_binding)
    monkeypatch.setattr(svc, "compute_skill_dir_hash", lambda _path: "d" * 64)
    monkeypatch.setattr(svc.agent_run_service, "prepare_agent_run_creation_scope", prepare_scope)
    monkeypatch.setattr(svc.agent_run_service, "create_agent_run_input_message", create_input)
    monkeypatch.setattr(svc.agent_run_service, "persist_agent_run_record", persist)

    skill_dir = tmp_path / "dept-work-report"
    skill_dir.mkdir()
    result = await service.start(
        uid="user-1",
        created_by_run_id="parent-run",
        description="读取 workspace 并生成月报",
        tool_call_id="call-1",
        skill_slugs=["dept-work-report"],
        skill_sources={"dept-work-report": skill_dir},
        skill_runtime_paths={"dept-work-report": "/home/gem/skills/dept-work-report"},
    )

    expected_thread = hash_id("pi_", "user-1:runtime-parent:assistant", length=64)
    assert result.run is child and result.created is True
    assert captured["child_thread_id"] == expected_thread
    assert captured["conversation"]["project_id"] == "project-1"
    assert captured["scope"]["run_type"] == "sandbox"
    assert captured["persist"]["runtime_scope_id"] == "runtime-parent"
    assert captured["persist"]["input_payload"] == {
        "model_spec": "provider:model",
        "tool_approval_mode": "always_trust",
        "runtime": {
            "executor": "pi",
            "tool_call_id": "call-1",
            "parent_thread_id": "thread-parent",
            "workdir_path": "projects/project-1",
            "skill_slugs": ["dept-work-report"],
            "skill_digests": {"dept-work-report": "d" * 64},
            "skill_runtime_paths": {"dept-work-report": "/home/gem/skills/dept-work-report"},
        },
    }
    assert db.commits == 1


@pytest.mark.asyncio
async def test_start_rejects_sandbox_parent_recursion_before_persisting(monkeypatch, tmp_path):
    service = svc.PiSandboxRunService(Db())
    service.run_repo.lock_run_for_user = lambda *_args: None

    async def lock(*_args):
        return SimpleNamespace(status="running", run_type="sandbox")

    service.run_repo.lock_run_for_user = lock

    with pytest.raises(ValueError, match="不能递归"):
        await service.start(
            uid="user-1",
            created_by_run_id="parent-run",
            description="读取文件",
            tool_call_id="call-1",
            skill_slugs=[],
            skill_sources={},
            skill_runtime_paths={},
        )
