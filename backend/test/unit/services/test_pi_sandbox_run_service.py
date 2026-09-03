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
    parent_conversation = SimpleNamespace(id=10, uid="user-1", project_id="project-1", status="active")
    project = SimpleNamespace(id="project-1", workdir_path="projects/project-1")
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

        async def lock_conversation_by_thread_id(self, thread_id):
            assert thread_id == "thread-parent"
            return parent_conversation

        async def add_conversation(self, **kwargs):
            captured["conversation"] = kwargs
            return SimpleNamespace(id=20, status="active", **kwargs)

    class ProjectRepo:
        async def lock_active_for_user(self, project_id, uid):
            assert (project_id, uid) == ("project-1", "user-1")
            return project

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
    service.project_repo = ProjectRepo()
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
@pytest.mark.parametrize(
    ("parent_status", "active_project", "message"),
    [
        ("deleted", True, "父运行任务的 Conversation 不存在"),
        ("active", False, "父运行任务的 Project 不存在"),
    ],
)
async def test_start_rejects_deleted_parent_scope(parent_status, active_project, message):
    service = svc.PiSandboxRunService(Db())
    parent_run = SimpleNamespace(
        id="parent-run",
        run_type="chat",
        agent_slug="assistant",
        conversation_thread_id="thread-parent",
        conversation_id=10,
        status="running",
    )
    parent_conversation = SimpleNamespace(
        id=10,
        uid="user-1",
        project_id="project-1",
        status=parent_status,
    )

    async def lock_run(*_args):
        return parent_run

    async def get_parent(*_args):
        return SimpleNamespace(**{**parent_conversation.__dict__, "status": "active"})

    async def lock_parent(*_args):
        return parent_conversation

    async def lock_project(*_args):
        return SimpleNamespace(id="project-1", workdir_path="projects/project-1") if active_project else None

    service.run_repo.lock_run_for_user = lock_run
    service.conv_repo.get_conversation_by_id = get_parent
    service.conv_repo.lock_conversation_by_thread_id = lock_parent
    service.project_repo.lock_active_for_user = lock_project

    with pytest.raises(ValueError, match=message):
        await service.start(
            uid="user-1",
            created_by_run_id="parent-run",
            description="读取文件",
            tool_call_id="call-1",
            skill_slugs=[],
            skill_sources={},
            skill_runtime_paths={},
        )


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
