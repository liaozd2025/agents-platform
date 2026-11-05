"""真实 PostgreSQL 与执行图验证运行边界的角色预加载。"""

from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import delete, select

from test.integration.support.module_audit import isolated_audit_store
from yuxi.modules.agents.models.definitions import Agent
from yuxi.modules.agents.models.runs import AgentRun
from yuxi.modules.agents.models.sessions import Session
from yuxi.modules.agents.models.turns import AgentTurn
from yuxi.modules.agents.runtime.agent_backends.chatbot.context import ChatBotContext
from yuxi.modules.agents.runtime.middlewares.authorization import AgentExecutionRevoked, RuntimeAuthorizationMiddleware
from yuxi.modules.extensions.skills.models import Skill
from yuxi.modules.identity.models import Role, RolePermission, User, UserRoleAssignment
from yuxi.modules.workspace.models import Project

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


@pytest.mark.parametrize("with_run", [False, True])
async def test_model_boundary_loads_roles_and_observes_revocation(tmp_path, monkeypatch, with_run):
    """新数据库会话加载角色，模型轮次成功且后续撤权立即生效。"""
    monkeypatch.setenv("YUXI_SKILL_DATA_DIR", str(tmp_path / "sources"))
    monkeypatch.setenv("YUXI_SKILL_PROJECTION_DIR", str(tmp_path / "projections"))
    source = tmp_path / "sources/shared/runtime-skill"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# Runtime skill\n")
    requests = []

    class ProbeModel(BaseChatModel):
        """只记录是否真正到达模型调用，无外部请求。"""

        @property
        def _llm_type(self):
            """标识本地回归模型。"""
            return "runtime-role-probe"

        def _generate(self, messages, **kwargs):
            """本测试仅执行异步路径。"""
            raise AssertionError("unexpected synchronous model call")

        async def _agenerate(self, messages, **kwargs):
            """返回可核验的模型结果。"""
            requests.append(messages)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ROLE_REFRESH_OK"))])

    async with isolated_audit_store(monkeypatch) as (_, sessions):
        async with sessions() as db:
            role = Role(
                code="runtime-role", name="Runtime", default_scope_type="self", permissions=[RolePermission(permission_key="skill:use")]
            )
            db.add(
                User(uid="runtime-user", username="runtime-user", password_hash="fixture", role_assignments=[UserRoleAssignment(role=role)])
            )
            db.add(
                Agent(
                    slug="runtime-agent",
                    name="Runtime",
                    backend_id="ChatbotAgent",
                    created_by="runtime-user",
                    visibility="private",
                    share_config={"version": 2, "read_scope": None, "manage_scope": None},
                )
            )
            db.add(
                Skill(
                    slug="runtime-skill",
                    name="Runtime",
                    source_type="upload",
                    description="fixture",
                    dir_path="shared/runtime-skill",
                    share_config={"version": 2, "read_scope": None, "manage_scope": None},
                    created_by="runtime-user",
                    enabled=True,
                )
            )
            db.add(
                Project(
                    id="runtime-project",
                    uid="runtime-user",
                    name="Runtime",
                    selection_status="implicit",
                    directory_mode="managed",
                    workdir_path="projects/runtime",
                )
            )
            await db.flush()
            db.add(
                Session(
                    thread_id="runtime-thread",
                    tree_root_thread_id="runtime-thread",
                    uid="runtime-user",
                    agent_id="runtime-agent",
                    status="active",
                    project_id="runtime-project",
                )
            )
            await db.flush()
            if with_run:
                db.add(AgentTurn(id="runtime-turn", thread_id="runtime-thread", uid="runtime-user"))
                await db.flush()
                db.add(
                    AgentRun(
                        id="runtime-run",
                        thread_id="runtime-thread",
                        runtime_scope_id="runtime-thread",
                        turn_id="runtime-turn",
                        agent_slug="runtime-agent",
                        uid="runtime-user",
                    )
                )
            await db.commit()
        context = ChatBotContext(
            run_id="runtime-run" if with_run else None,
            uid="runtime-user",
            thread_id="runtime-thread",
            agent_slug="runtime-agent",
            tools=[],
            mcps=[],
            knowledges=[],
            skills=["runtime-skill"],
            preload_skills=[],
        )
        middleware = RuntimeAuthorizationMiddleware()
        graph = create_agent(model=ProbeModel(), middleware=[middleware])
        result = await graph.ainvoke({"messages": [HumanMessage(content="probe")]}, context=context)
        assert result["messages"][-1].content == "ROLE_REFRESH_OK"
        assert len(requests) == 1
        assert context.skills == ["runtime-skill"]

        async with sessions() as db:
            await db.execute(delete(RolePermission).where(RolePermission.permission_key == "skill:use"))
            await db.commit()
        await middleware.abefore_model({}, SimpleNamespace(context=context))
        assert context.skills == []
        assert context._resource_selections["skills"] == ["runtime-skill"]

        async with sessions() as db:
            user = await db.scalar(select(User).where(User.uid == "runtime-user"))
            user.is_deleted = 1
            await db.commit()
        with pytest.raises(AgentExecutionRevoked, match="账号已失效"):
            await graph.ainvoke({"messages": [HumanMessage(content="blocked")]}, context=context)
        assert len(requests) == 1
