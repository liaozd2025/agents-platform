"""文件读取及分页知识文档结果不能被再次卸载而形成循环。"""

from types import SimpleNamespace

import pytest
from deepagents.backends import CompositeBackend
from langchain_core.messages import ToolMessage

from yuxi.modules.agents.runtime.middlewares.filesystem import create_agent_filesystem_middleware


@pytest.mark.parametrize("tool_name", ["read_file", "open_kb_document"])
def test_bounded_read_results_stay_inline_and_do_not_write_another_file(tool_name):
    """极低卸载阈值也保留读结果，避免 read→offload→read 的循环。"""

    class Backend:
        def write(self, *args):
            raise AssertionError("有界读取结果不能再次卸载")

    middleware = create_agent_filesystem_middleware(
        1,
        backend=CompositeBackend(default=Backend(), routes={}, artifacts_root="/home/user/workspace/projects/test/outputs"),
    )
    result = ToolMessage(content="x" * 100, name=tool_name, tool_call_id="read-call")
    request = SimpleNamespace(
        tool_call={"name": tool_name, "args": {"file_path": "/home/user/skills/demo/SKILL.md"}}, runtime=SimpleNamespace()
    )
    assert middleware.wrap_tool_call(request, lambda _: result) is result
