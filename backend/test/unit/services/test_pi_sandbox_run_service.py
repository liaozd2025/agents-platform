import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from yuxi.services import pi_sandbox_run_service as svc
from yuxi.utils.hash_utils import hash_id


@pytest.mark.asyncio
async def test_skill_digest_runs_outside_event_loop(monkeypatch, tmp_path):
    """目录摘要计算阻塞时，事件循环仍应能够调度心跳任务。"""

    started = threading.Event()

    def blocking_hash(_path):
        started.set()
        time.sleep(0.3)
        return "d" * 64

    monkeypatch.setattr(svc, "compute_skill_dir_hash", blocking_hash)
    task = asyncio.create_task(svc._compute_skill_digests({"report": tmp_path}))
    await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=1)
    finished = asyncio.Event()
    ticks = 0

    async def heartbeat_probe():
        nonlocal ticks
        while not finished.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    probe = asyncio.create_task(heartbeat_probe())
    assert await task == {"report": "d" * 64}
    finished.set()
    await probe
    assert ticks >= 2


class Db:
    def __init__(self):
        self.commits = 0

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source,legacy", [(" ", None), ("edit-a", False)])
async def test_invalid_history_request_does_not_create_run(source, legacy):
    """空来源或相互矛盾的续接参数不能写入运行请求。"""
    db = Db()
    with pytest.raises(ValueError, match="来源"):
        await svc.PiSandboxRunService(db).start(
            uid="user",
            created_by_run_id="parent",
            description="修正原文件",
            tool_call_id="call",
            skill_slugs=[],
            skill_sources={},
            skill_runtime_paths={},
            source_run_id=source,
            continue_session=legacy,
        )
    assert db.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("source_run_id,continue_session", [(None, None), ("edit-a", None), (None, True)])
async def test_start_inherits_parent_runtime_project_model_and_locked_skills(
    monkeypatch, tmp_path, source_run_id, continue_session
):
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
    children = []

    class RunRepo:
        async def require_pi_scope_available(self, creator_run):
            assert creator_run is parent
            if children:
                raise ValueError("不能再次委派")

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
        source_run_id=source_run_id,
        continue_session=continue_session,
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
            "source_run_id": source_run_id,
            "continue_session": bool(source_run_id or continue_session),
        },
    }
    assert db.commits == 1

    children.append(SimpleNamespace(id="uncertain-run", run_type="sandbox", error_type="execution_unknown"))
    monkeypatch.setattr(svc.agent_run_service, "prepare_agent_run_creation_scope", prepare_scope)
    with pytest.raises(ValueError, match="不能再次委派"):
        await service.start(
            uid="user-1",
            created_by_run_id="parent-run",
            description="重新生成整份报告",
            tool_call_id="new-call",
            skill_slugs=[],
            skill_sources={},
            skill_runtime_paths={},
        )
    assert db.commits == 1

    child.input_payload = captured["persist"]["input_payload"]
    original_runtime = dict(child.input_payload["runtime"])

    async def replay_scope(**_kwargs):
        return SimpleNamespace(existing_run=child)

    monkeypatch.setattr(svc.agent_run_service, "prepare_agent_run_creation_scope", replay_scope)
    replay = await service.start(
        uid="user-1",
        created_by_run_id="parent-run",
        description="重放时的新描述",
        tool_call_id="call-1",
        skill_slugs=["dept-work-report"],
        skill_sources={"dept-work-report": skill_dir},
        skill_runtime_paths={"dept-work-report": "/home/gem/skills/dept-work-report"},
        source_run_id="check-b",
    )
    assert replay.run is child and replay.created is False
    assert child.input_payload["runtime"] == original_runtime
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
        input_payload={},
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


@pytest.mark.asyncio
async def test_default_subagent_cannot_create_pi_even_when_tool_is_called_directly():
    """绕过工具可见性直接调用服务时，仍必须交回主图审批。"""
    db = Db()
    service = svc.PiSandboxRunService(db)

    async def lock(*_args):
        return SimpleNamespace(status="running", run_type="subagent", input_payload={"tool_approval_mode": "default"})

    service.run_repo.lock_run_for_user = lock
    with pytest.raises(ValueError, match="交回主智能体"):
        await service.start(
            uid="user",
            created_by_run_id="subagent",
            description="生成文件",
            tool_call_id="call",
            skill_slugs=[],
            skill_sources={},
            skill_runtime_paths={},
        )
    assert db.commits == 0
