"""引用证据捕获、裁切与 checkpoint 生命周期的回归检查。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt
from yuxi.agents.middlewares.citations import (
    NO_SOURCE_PROMPT,
    CitationMiddleware,
    collapse_duplicate_citations,
    knowledge_citation_sources,
    merge_citation_sources,
    strip_unbound_citations,
)
from yuxi.agents.middlewares.subagent_task import _json_tool_command, _task_result_response


class _RequestStub:
    """只承载引用中间件读写的最小请求替身。"""

    def __init__(self, sources):
        self.state = {"citation_sources": sources}
        self.system_message = SystemMessage(content="原始系统提示")

    def override(self, system_message):
        return SimpleNamespace(state=self.state, system_message=system_message)


def test_unbound_citations_are_stripped_when_no_source_was_obtained():
    """回归：模型在没有任何来源时自造 file:// 身份，落库前必须被剥离。"""
    fabricated = '<cite source="file:///home/gem/skills/image-gen/SKILL.md" type="file">1</cite>'
    content = f"可以的，我可以使用图片生成技能。{fabricated}请告诉我你想要生成什么样的图片。"
    assert strip_unbound_citations(content, []) == "可以的，我可以使用图片生成技能。请告诉我你想要生成什么样的图片。"
    assert strip_unbound_citations(content, None) == "可以的，我可以使用图片生成技能。请告诉我你想要生成什么样的图片。"
    assert strip_unbound_citations("无引用正文", []) == "无引用正文"


def test_bound_source_and_safe_url_survive_stripping():
    """已绑定来源与可直接打开的网页链接不得被误删，保留原编号。"""
    bound = knowledge_citation_sources([{"kb_id": "kb-a", "file_id": "file-1", "content": "原文"}])
    kept = '<cite source="kb://kb-a/file-1" type="file">1</cite>'
    linked = '<cite source="https://example.com/a" type="url">2</cite>'
    fabricated = '<cite source="file:///home/gem/skills/x/SKILL.md" type="file">3</cite>'
    content = f"甲{kept}乙{linked}假{fabricated}丙"
    assert strip_unbound_citations(content, bound) == f"甲{kept}乙{linked}假丙"
    # 无 source 属性的畸形标签同样不保留
    assert strip_unbound_citations('结论<cite type="file">1</cite>', bound) == "结论"


def test_adjacent_duplicate_citations_are_collapsed():
    """回归：同一处为同一份原文连贴多个标签，落库前只保留一个徽标。"""
    bound = knowledge_citation_sources([{"kb_id": "kb-a", "file_id": "file-1", "content": "原文"}])
    first = '<cite source="kb://kb-a/file-1" type="file">1</cite>'
    second = '<cite source="kb://kb-a/file-1" type="file">6</cite>'
    other = '<cite source="https://example.com/a" type="url">2</cite>'

    assert collapse_duplicate_citations(f"共处{first}{second}。") == f"共处{first}。"
    assert collapse_duplicate_citations(f"共处{first} {second}。") == f"共处{first}。"
    assert collapse_duplicate_citations(f"{first}{second}{first}") == first
    # 不同来源相邻、同一来源在正文他处被再次引用都属于正常复引
    assert collapse_duplicate_citations(f"甲{first}{other}乙") == f"甲{first}{other}乙"
    assert collapse_duplicate_citations(f"甲{first}乙{first}丙") == f"甲{first}乙{first}丙"
    assert collapse_duplicate_citations("没有引用标签") == "没有引用标签"

    fabricated = '<cite source="file:///tmp/x.md" type="file">3</cite>'
    cleaned = CitationMiddleware()._drop_unbound_citations(
        AIMessage(content=f"共处{first}{fabricated}{second}"), bound
    )
    assert cleaned.content == f"共处{first}"


def test_response_citations_are_cleaned_on_both_model_paths():
    """同步与异步模型返回都要经过过滤，未绑定标签不会进入 State。"""
    middleware = CitationMiddleware()
    bound = knowledge_citation_sources([{"kb_id": "kb-a", "file_id": "file-1", "content": "原文"}])
    kept = '<cite source="kb://kb-a/file-1" type="file">1</cite>'
    fabricated = '<cite source="file:///home/gem/skills/x/SKILL.md" type="file">2</cite>'
    response = AIMessage(content=f"甲{kept}假{fabricated}")

    assert middleware.wrap_model_call(_RequestStub(bound), lambda _request: response).content == f"甲{kept}假"
    assert middleware.wrap_model_call(_RequestStub([]), lambda _request: response).content == "甲假"
    assert middleware._drop_unbound_citations(response, bound) is not response
    # 没有引用标签的回答保持原对象，不做无谓重建
    plain = AIMessage(content="纯正文")
    assert middleware._drop_unbound_citations(plain, []) is plain

    blocks = AIMessage(content=[{"type": "text", "text": f"甲{fabricated}"}, {"type": "text", "text": "乙"}])
    assert middleware._drop_unbound_citations(blocks, []).content == [
        {"type": "text", "text": "甲"},
        {"type": "text", "text": "乙"},
    ]


@pytest.mark.asyncio
async def test_async_model_path_cleans_fabricated_citations():
    """异步模型调用走同一条清洗路径。"""
    middleware = CitationMiddleware()
    response = AIMessage(content='结论<cite source="file:///tmp/x.md" type="file">1</cite>')

    async def handler(_request):
        return response

    cleaned = await middleware.awrap_model_call(_RequestStub([]), handler)
    assert cleaned.content == "结论"


def test_prompt_states_absence_of_sources_and_lists_identities():
    """有来源时提供身份清单，没有来源时显式禁止输出引用标签。"""
    middleware = CitationMiddleware()
    bound = knowledge_citation_sources([{"kb_id": "kb-a", "file_id": "file-1", "content": "原文"}])

    def system_text(sources):
        content = middleware._with_sources(_RequestStub(sources)).system_message.content
        if isinstance(content, str):
            return content
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))

    assert "kb://kb-a/file-1" in system_text(bound)
    assert NO_SOURCE_PROMPT in system_text([])


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
