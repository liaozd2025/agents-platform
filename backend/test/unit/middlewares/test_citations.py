"""引用证据捕获、裁切与 checkpoint 生命周期的回归检查。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt
from yuxi.agents.middlewares.citations import CitationMiddleware, knowledge_citation_sources, merge_citation_sources
from yuxi.agents.middlewares.subagent_task import _json_tool_command, _task_result_response


def test_sources_keep_originals_and_do_not_invent_file_identity():
    sources = knowledge_citation_sources(
        [
            {
                "kb_id": "kb-a",
                "file_id": "file-a",
                "id": "chunk-a",
                "content": "原文",
                "metadata": {"source": "同名.md"},
            },
            {"kb_id": "kb-a", "file_id": "file-b", "content": "另一原文", "metadata": {"source": "同名.md"}},
            {"content": "无身份的正文", "metadata": {"source": "同名.md"}},
        ]
    )
    assert [s["source"] for s in sources] == ["kb://kb-a/file-a", "kb://kb-a/file-b"]
    assert sources[0]["excerpts"] == [{"text": "原文", "chunk_id": "chunk-a"}]
    assert merge_citation_sources(sources, sources) == sources
    message = ToolMessage(
        json.dumps({"results": [{"url": "https://example.com", "title": "网页", "content": "模型摘要"}]}),
        tool_call_id="web",
    )
    result = CitationMiddleware()._capture("web_search", message)
    assert result.update["citation_sources"][0]["excerpts"] == []
    assert CitationMiddleware()._capture("execute", message) is message
    raw = message.model_copy(
        update={
            "content": json.dumps(
                {
                    "results": [
                        {"url": "https://example.com/raw", "content": "摘要", "raw_content": "网页原文"},
                        {"url": "javascript:alert(1)", "raw_content": "不安全来源"},
                    ]
                }
            )
        }
    )
    raw_sources = CitationMiddleware()._capture("web_search", raw).update["citation_sources"]
    assert len(raw_sources) == 1
    assert raw_sources[0]["excerpts"] == [{"text": "网页原文"}]
    error = message.model_copy(update={"status": "error"})
    assert CitationMiddleware()._capture("web_search", error) is error


def test_task_and_async_results_transfer_sources_without_reusing_local_numbers():
    source = knowledge_citation_sources([{"kb_id": "kb", "file_id": "a", "content": "原文 A"}])[0]
    result = {"output": '<cite source="kb://kb/a" type="file">1</cite>', "citation_sources": [source]}
    run = {"run_id": "child", "child_thread_id": "thread"}
    commands = [
        _task_result_response(result, "call", run),
        _json_tool_command({"result": result}, "call", subagent_run=run),
    ]
    for command in commands:
        assert command.update["messages"][0].artifact["citation_sources"] == [source]
        captured = CitationMiddleware()._capture("task", command)
        assert captured.update["citation_sources"] == [source]
    assert json.loads(commands[1].update["messages"][0].content)["citation_sources"] == [source]


class _CitationModel(BaseChatModel):
    """真实 Graph 的确定性模型：读文档后等待确认，再输出回答。"""

    @property
    def _llm_type(self):
        return "citation-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        last = messages[-1]
        if isinstance(last, HumanMessage) and last.content == "新问题":
            reply = AIMessage(content="本轮没有来源")
        elif isinstance(last, HumanMessage):
            reply = AIMessage(content="", tool_calls=[{"id": "read", "name": "open_kb_document", "args": {}}])
        elif isinstance(last, ToolMessage) and last.name == "open_kb_document":
            assert "kb://kb/file" in str(messages[0].content)
            reply = AIMessage(content="", tool_calls=[{"id": "ask", "name": "ask_user_question", "args": {}}])
        else:
            reply = AIMessage(content='结论<cite source="kb://kb/file" type="file">1</cite>')
        return ChatResult(generations=[ChatGeneration(message=reply)])


@pytest.mark.asyncio
async def test_graph_resume_retains_evidence_and_new_input_clears_it():
    @tool
    def open_kb_document() -> dict:
        """返回原文工具协议。"""
        return {"kb_id": "kb", "file_id": "file", "content": "真实原文", "start_line": 1, "end_line": 1}

    @tool
    def ask_user_question() -> str:
        """模拟用户确认。"""
        return interrupt("继续？")

    graph = create_agent(
        _CitationModel(),
        tools=[open_kb_document, ask_user_question],
        middleware=[CitationMiddleware()],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "citations"}}
    result = await graph.ainvoke({"messages": [HumanMessage(content="查询")]}, config)
    assert result["__interrupt__"]
    sources = result["citation_sources"]
    assert sources[0]["excerpts"][0]["text"] == "真实原文"
    resumed = await graph.ainvoke(Command(resume="继续"), config)
    assert resumed["citation_sources"] == sources
    assert "结论" in resumed["messages"][-1].content
    fresh = await graph.ainvoke({"messages": [HumanMessage(content="新问题")]}, config)
    assert fresh["citation_sources"] == []


@pytest.mark.asyncio
async def test_filesystem_eviction_preserves_captured_originals(tmp_path):
    from deepagents.backends import FilesystemBackend
    from yuxi.agents.backends import create_agent_filesystem_middleware

    text = "真实原文" * 4000
    response = ToolMessage(
        json.dumps({"kb_id": "kb", "results": [{"file_id": "file", "content": text}]}),
        tool_call_id="read-large",
    )
    request = SimpleNamespace(tool_call={"name": "query_kb"})
    citations = CitationMiddleware()
    filesystem = create_agent_filesystem_middleware(
        100, backend=FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    )

    async def original(_request):
        return response

    async def capture(inner_request):
        return await citations.awrap_tool_call(inner_request, original)

    result = await filesystem.awrap_tool_call(request, capture)
    assert result.update["citation_sources"][0]["excerpts"][0]["text"] == text
    assert text not in result.update["messages"][0].content
