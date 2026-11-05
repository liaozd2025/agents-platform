"""真实 PostgreSQL 独立会话验证输入和协作的组织共享权限。"""

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from test.integration.services.agent_input_fixtures import SCOPE, sessions, start_input_run  # noqa: F401
from yuxi.modules.agents.input_messages import build_chat_input_message
from yuxi.modules.agents.models.definitions import Agent
from yuxi.modules.agents.models.feedback import AgentFeedbackRecord
from yuxi.modules.agents.models.inputs import AgentInput
from yuxi.modules.agents.models.runs import AgentRun
from yuxi.modules.agents.models.sessions import Session
from yuxi.modules.agents.runtime.middlewares.authorization import AgentExecutionRevoked
from yuxi.modules.agents.services import cooperation, feedback, inputs
from yuxi.modules.identity.models import Department, User
from yuxi.modules.workspace.services.bindings import WorkdirBinding
from yuxi.shared.errors import ResourceNotFoundError

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


@pytest_asyncio.fixture
async def department_store(sessions, monkeypatch):  # noqa: F811
    """复用隔离 Schema，仅替代本次验收外的文件物化与 worker 投递。"""
    async with sessions() as db:
        db.add_all(
            [
                Department(id=2, name="成员部门", parent_id=1, path="/1/2/"),
                Department(id=3, name="其他部门", parent_id=1, path="/1/3/"),
            ]
        )
        await db.flush()
        user = await db.scalar(select(User).where(User.uid == SCOPE.uid))
        user.department_id = 2
        main = await db.scalar(select(Agent).where(Agent.slug == "main"))
        main.created_by = "another-owner"
        main.visibility = "shared"
        main.share_config = {
            "version": 2,
            "read_scope": {"access_level": "department", "department_ids": [1], "user_uids": []},
            "manage_scope": None,
        }
        db.add(
            Agent(
                slug="other-department",
                backend_id="ChatbotAgent",
                name="其他部门智能体",
                created_by="another-owner",
                visibility="shared",
                config_json={"context": {"model": "test:chat"}},
                share_config={
                    "version": 2,
                    "read_scope": {"access_level": "department", "department_ids": [3], "user_uids": []},
                    "manage_scope": None,
                },
            )
        )
        await db.commit()

    async def no_dispatch(**kwargs):
        """本用例只验收接收事务，不启动外部执行。"""

    async def binding(*, agent_session, uid, project, db):
        """保留真实 Project 归属，免于创建宿主目录。"""
        return WorkdirBinding(
            session_record_id=agent_session.id,
            thread_id=agent_session.thread_id,
            uid=uid,
            project_id=project.id,
            workdir_path=project.workdir_path,
            directory_mode="linked",
        )

    monkeypatch.setattr(inputs, "dispatch_next_input", no_dispatch)
    monkeypatch.setattr(cooperation, "dispatch_next_input", no_dispatch)
    monkeypatch.setattr(inputs, "resolve_session_workdir_binding", binding)
    return sessions


async def test_create_thread_loads_department_ancestors(department_store):
    """创建入口接受上级组织共享，并从新事务回读会话和输入。"""
    async with department_store() as db:
        result = await inputs.create_thread(
            db=db,
            scope=SCOPE,
            agent_slug="main",
            thread_id="department-created",
            idempotency_key="create",
            project_id="input-project",
            messages=[build_chat_input_message("部门创建")],
        )
    async with department_store() as db:
        thread = await db.scalar(select(Session).where(Session.thread_id == "department-created"))
        item = await db.get(AgentInput, result["input_id"])
        assert thread.agent_id == "main" and thread.uid == SCOPE.uid
        assert item.thread_id == thread.thread_id and item.uid == SCOPE.uid and item.status == "pending"


async def test_append_input_loads_department_ancestors(department_store):
    """追加入口不能依赖创建请求留下的 ORM 动态属性。"""
    async with department_store() as db:
        result = await inputs.accept_message(
            db=db,
            scope=SCOPE,
            thread_id="input-thread",
            idempotency_key="append",
            mode="follow_up",
            source="test",
            messages=[build_chat_input_message("部门追加")],
        )
    async with department_store() as db:
        item = await db.get(AgentInput, result["input_id"])
        assert item.thread_id == "input-thread" and item.agent_slug == "main" and item.uid == SCOPE.uid


async def test_cooperation_directory_loads_department_ancestors(department_store):
    """协作目录展示授权上级组织的 Agent，并排除其他部门。"""
    run = await start_input_run(department_store)
    async with department_store() as db:
        result = await cooperation.SessionCooperationService(
            db,
            run_id=run.id,
            uid=SCOPE.uid,
            worker_id="owner",
        ).list_agents()
        assert {item["id"] for item in result["agents"]} == {"main"}


async def test_cooperation_creation_loads_department_ancestors(department_store):
    """协作创建经真实接收路径，成员和输入均保持当前用户归属。"""
    run = await start_input_run(department_store)
    async with department_store() as db:
        result = await cooperation.SessionCooperationService(
            db,
            run_id=run.id,
            uid=SCOPE.uid,
            worker_id="owner",
        ).create_session(name="member", description="部门协作", call_id="member", agent_id="main")
    async with department_store() as db:
        member = await db.scalar(select(Session).where(Session.thread_id == result["session_id"]))
        item = await db.get(AgentInput, result["input_id"])
        assert member.parent_thread_id == "input-thread" and member.agent_id == "main" and member.uid == SCOPE.uid
        assert item.thread_id == member.thread_id and item.uid == SCOPE.uid and item.status == "pending"


async def test_feedback_mark_loads_department_ancestors(department_store):
    """独立工具事务接受上级组织共享，并回读当前 Run 的改进记录。"""
    run = await start_input_run(department_store)
    async with department_store() as db:
        result = await feedback.mark_run_for_improvement(
            db=db,
            run_id=run.id,
            uid=SCOPE.uid,
            thread_id="input-thread",
            worker_id="owner",
            tool_call_id="department-feedback",
            reason="部门共享智能体的改进线索",
        )
    async with department_store() as db:
        records = list((await db.scalars(select(AgentFeedbackRecord))).all())
        assert len(records) == 1
        record = records[0]
        assert record.id == result["id"] and record.run_id == run.id
        assert record.source == "agent" and record.tool_call_id == "department-feedback"
        assert record.reason == "部门共享智能体的改进线索"
        persisted_run = await db.get(AgentRun, run.id)
        assert persisted_run.status == "running" and persisted_run.worker_id == "owner"


@pytest.mark.parametrize("revocation", ["other_department", "deleted_user"])
async def test_feedback_mark_rejects_revoked_access_without_writes(department_store, revocation):
    """运行后撤销组织共享或删除账号，工具事务不得保存改进记录。"""
    run = await start_input_run(department_store)
    async with department_store() as db:
        if revocation == "other_department":
            agent = await db.scalar(select(Agent).where(Agent.slug == "main"))
            agent.share_config = {
                "version": 2,
                "read_scope": {"access_level": "department", "department_ids": [3], "user_uids": []},
                "manage_scope": None,
            }
        else:
            user = await db.scalar(select(User).where(User.uid == SCOPE.uid))
            user.is_deleted = 1
        await db.commit()
    async with department_store() as db:
        with pytest.raises(AgentExecutionRevoked, match="执行账号或 Agent 使用权限已撤销"):
            await feedback.mark_run_for_improvement(
                db=db,
                run_id=run.id,
                uid=SCOPE.uid,
                thread_id="input-thread",
                worker_id="owner",
                tool_call_id="revoked-feedback",
                reason="不能持久化的线索",
            )
    async with department_store() as db:
        assert await db.scalar(select(func.count()).select_from(AgentFeedbackRecord)) == 0
        persisted_run = await db.get(AgentRun, run.id)
        assert persisted_run.status == "running" and persisted_run.worker_id == "owner"


@pytest.mark.parametrize("operation", ["create", "append", "cooperate"])
async def test_other_department_is_rejected_without_writes(department_store, operation):
    """直接指定其他部门 Agent 也被拒绝，不留下会话或输入。"""
    run = await start_input_run(department_store) if operation == "cooperate" else None
    if operation == "append":
        async with department_store() as db:
            thread = await db.scalar(select(Session).where(Session.thread_id == "input-thread"))
            thread.agent_id = "other-department"
            await db.commit()
    async with department_store() as db:
        counts = (await db.scalar(select(func.count()).select_from(Session)), await db.scalar(select(func.count()).select_from(AgentInput)))
    async with department_store() as db:
        if operation == "create":
            with pytest.raises(ResourceNotFoundError, match="智能体不存在"):
                await inputs.create_thread(
                    db=db,
                    scope=SCOPE,
                    agent_slug="other-department",
                    thread_id="denied",
                    idempotency_key="denied",
                    project_id="input-project",
                )
        elif operation == "append":
            with pytest.raises(ResourceNotFoundError, match="智能体不存在"):
                await inputs.accept_message(
                    db=db,
                    scope=SCOPE,
                    thread_id="input-thread",
                    idempotency_key="denied",
                    mode="follow_up",
                    source="test",
                    messages=[build_chat_input_message("禁止")],
                )
        else:
            with pytest.raises(ValueError, match="无权限运行"):
                await cooperation.SessionCooperationService(
                    db,
                    run_id=run.id,
                    uid=SCOPE.uid,
                    worker_id="owner",
                ).create_session(name="denied", description="禁止", call_id="denied", agent_id="other-department")
    async with department_store() as db:
        assert counts == (
            await db.scalar(select(func.count()).select_from(Session)),
            await db.scalar(select(func.count()).select_from(AgentInput)),
        )


@pytest.mark.parametrize("operation", ["create", "append", "directory", "cooperate"])
async def test_deleted_execution_user_is_rejected(department_store, operation):
    """完整加载身份后仍拒绝软删除账号。"""
    run = await start_input_run(department_store) if operation in {"directory", "cooperate"} else None
    async with department_store() as db:
        user = await db.scalar(select(User).where(User.uid == SCOPE.uid))
        user.is_deleted = 1
        await db.commit()
    async with department_store() as db:
        if operation == "create":
            with pytest.raises(ResourceNotFoundError):
                await inputs.create_thread(
                    db=db,
                    scope=SCOPE,
                    agent_slug="main",
                    thread_id="deleted",
                    idempotency_key="deleted",
                    project_id="input-project",
                )
        elif operation == "append":
            with pytest.raises(ResourceNotFoundError):
                await inputs.accept_message(
                    db=db,
                    scope=SCOPE,
                    thread_id="input-thread",
                    idempotency_key="deleted",
                    mode="follow_up",
                    source="test",
                    messages=[build_chat_input_message("禁止")],
                )
        else:
            service = cooperation.SessionCooperationService(db, run_id=run.id, uid=SCOPE.uid, worker_id="owner")
            with pytest.raises(ValueError, match="账号已失效"):
                if operation == "directory":
                    await service.list_agents()
                else:
                    await service.create_session(name="deleted", description="禁止", call_id="deleted")
