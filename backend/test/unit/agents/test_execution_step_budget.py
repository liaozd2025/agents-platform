"""真实中间件装配在固定预算下完成有限任务，撤权仍阻止副作用。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from yuxi.modules.agents.runtime.agent_backends.chatbot import graph as chatbot_graph
from yuxi.modules.agents.runtime.agent_backends.chatbot.context import ChatBotContext
from yuxi.modules.agents.runtime.base import BaseAgent, GraphExecutionResult
from yuxi.modules.agents.runtime.middlewares import authorization
from yuxi.modules.agents.runtime.middlewares.cooperation import CooperationMiddleware

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize("scenario", ["complete", "revoked", "provider_failure", "steer", "approval_steer"])
async def test_finite_task_completes_with_300_steps_and_rechecks_authorization(monkeypatch, scenario):
    """替换外部 I/O，保留模型、工具、协作、审批和授权的真实装配。"""
    effects = []
    revoked = False
    pending_steer = False

    tool_name = "execute" if scenario in {"revoked", "approval_steer"} else "probe"

    @tool(tool_name)
    def probe(index: int) -> str:
        """读取一个独立文件，记录实际业务结果。"""
        effects.append(index)
        return f"file-{index}-ok"

    class FiniteModel(BaseChatModel):
        """固定读取 45 个文件后结束，不失败也不重复重试。"""

        @property
        def _llm_type(self):
            """返回本地确定性模型类型。"""
            return "finite-task"

        def bind_tools(self, tools, **kwargs):
            """保留固定测试模型。"""
            return self

        def _generate(self, messages, **kwargs):
            """仅允许异步执行。"""
            raise NotImplementedError

        async def _agenerate(self, messages, **kwargs):
            """按已完成工具结果决定下一份文件或最终答复。"""
            nonlocal revoked, pending_steer
            count = sum(isinstance(message, ToolMessage) for message in messages)
            if scenario == "complete":
                assert any(message.id == "cooperation:7" for message in messages)
            revoked = scenario == "revoked"
            if scenario == "provider_failure":
                raise ValueError("fixture provider failed")
            message = AIMessage(content="45 files complete")
            if count < 45:
                message = AIMessage(content="", tool_calls=[{"name": tool_name, "args": {"index": count}, "id": f"file-{count}"}])
            if scenario == "approval_steer":
                pending_steer = True
                message = AIMessage(
                    content="", tool_calls=[{"name": "execute", "args": {"index": index}, "id": f"approval-{index}"} for index in range(2)]
                )
            return ChatResult(generations=[ChatGeneration(message=message)])

    @asynccontextmanager
    async def session():
        """隔离数据库连接，授权结论由独立可变身份决定。"""
        yield None

    async def actor(*args, **kwargs):
        """模拟模型请求期间撤销的身份。"""
        if revoked:
            raise authorization.AgentExecutionRevoked("fixture revoked")
        return SimpleNamespace(id=1), None, None

    context = ChatBotContext(
        uid="fixture",
        run_id="run",
        thread_id="budget",
        workdir_relative_path="projects/probe",
        tools=[tool_name],
        skills=[],
        mcps=[],
        knowledges=[],
        tool_approval_mode="default",
        max_execution_steps=300,
        model_retry_times=0,
        mcp_unavailable={"fixture": "offline"},
        registered_builtin_tool_names={tool_name},
        mcp_tool_servers={},
        mcp_tools={},
        skill_runtime_snapshot={"effective_skills": [], "preloaded_skills": [], "runtime_skills": {}, "skill_metadata": {}},
    )
    monkeypatch.setattr(authorization.pg_manager, "get_async_session_context", session)
    monkeypatch.setattr(authorization, "_require_execution_actor", actor)
    monkeypatch.setattr(authorization, "refresh_agent_context_resources", AsyncMock())
    monkeypatch.setattr(authorization, "available_mcp_servers", AsyncMock(return_value={}))
    monkeypatch.setattr(authorization, "sync_agent_context_skills", AsyncMock())

    async def should_yield(run_id):
        """独立模拟模型调用期间到达的接管输入。"""
        return pending_steer or scenario == "steer"

    monkeypatch.setattr("yuxi.modules.agents.services.runs.should_yield_for_steer", should_yield)
    monkeypatch.setattr(chatbot_graph, "create_memory_middleware", AsyncMock(return_value=None))
    cooperation = CooperationMiddleware()
    cooperation._identity_run_id, cooperation._identity_prompt = context.run_id, "fixture"
    monkeypatch.setattr(chatbot_graph, "create_cooperation_middleware", lambda context: cooperation)
    monkeypatch.setattr(
        "yuxi.modules.agents.services.cooperation.SessionCooperationService",
        lambda *args, **kwargs: SimpleNamespace(caller=AsyncMock(return_value=(None, SimpleNamespace(id="session")))),
    )

    async def mailbox(self, caller, *, after):
        """已消费游标之外才返回一次协作消息。"""
        if scenario != "complete" or after:
            return []
        return [SimpleNamespace(id=7, sender_thread_id="peer", kind="reply", content="READY", payload={})]

    monkeypatch.setattr("yuxi.modules.agents.runtime.middlewares.cooperation.CooperationRepository.mailbox", mailbox)
    model = FiniteModel()
    summary_factory = chatbot_graph.create_summary_middleware_from_context
    monkeypatch.setattr(
        chatbot_graph, "create_summary_middleware_from_context", lambda context, **kwargs: summary_factory(context, model=model, **kwargs)
    )
    middlewares = await chatbot_graph._build_middlewares(context, None)
    graph = create_agent(model=model, tools=[probe], middleware=middlewares, checkpointer=InMemorySaver())
    config = {"recursion_limit": 300, "configurable": {"thread_id": "budget"}}

    class FixtureAgent(BaseAgent):
        """复用真实流式入口，仅提供本地确定性图。"""

        async def get_graph(self, **kwargs):
            """返回已装配的生产中间件图。"""
            return graph

    async def run():
        """消费生产 v3 事件入口直至 checkpoint 结果。"""
        return [
            event async for event in FixtureAgent().stream_messages_with_state([HumanMessage(content="Read 45 files.")], context=context)
        ]

    if scenario == "revoked":
        with pytest.raises(authorization.AgentExecutionRevoked, match="fixture revoked"):
            await run()
        assert effects == []
        saved = await graph.aget_state(config)
        assert not saved.interrupts
        assert saved.values["messages"][-1].tool_calls[0]["name"] == "execute"
        assert saved.values["token_usage"]["run"]["model_call_count"] == 1
        assert saved.values["warnings"][0]["source"] == "mcp:fixture"
    elif scenario == "provider_failure":
        with pytest.raises(ValueError, match="fixture provider failed"):
            await run()
        saved = await graph.aget_state(config)
        assert effects == [] and len(saved.values["messages"]) == 1
        assert saved.values["warnings"][0]["source"] == "mcp:fixture"
    elif scenario == "steer":
        await run()
        saved = await graph.aget_state(config)
        assert context.steer_before_model and not saved.next and not saved.interrupts
        assert effects == [] and len(saved.values["messages"]) == 1
        assert saved.values["token_usage"]["run"]["model_call_count"] == 0
    elif scenario == "approval_steer":
        await run()
        assert (await graph.aget_state(config)).interrupts
        decision = Command(resume={"decisions": [{"type": "approve"}, {"type": "reject", "message": "skip"}]})
        async for _ in FixtureAgent().stream_resume_with_state(decision, context=context):
            pass
        saved = await graph.aget_state(config)
        assert effects == [] and not saved.next and not saved.interrupts
        assert not context.steer_before_model
    else:
        events = await run()
        assert isinstance(events[-1], GraphExecutionResult)
        saved = await graph.aget_state(config)
        assert effects == list(range(45))
        assert saved.values["messages"][-1].content == "45 files complete"
        assert saved.values["cooperation_cursor"] == 7
        assert sum(message.id == "cooperation:7" for message in saved.values["messages"]) == 1
        assert not saved.next and not saved.interrupts
        assert context.max_execution_steps == config["recursion_limit"] == 300
