"""验收辅助函数不能把错误成功响应变成跳过。"""

import httpx
import pytest

from test.integration.api.helpers import create_default_agent_thread

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize("agent", [{}, {"slug": "chatbot"}])
async def test_thread_helper_rejects_missing_agent_instead_of_skipping(agent):
    """错误的成功响应必须失败；正常响应仍能创建可验收线程。"""

    def respond(request):
        """仅替换待验证的 HTTP 协议响应。"""
        if request.method == "GET":
            return httpx.Response(200, json={"agent": agent})
        assert request.url.path == "/api/v1/agents/sessions"
        return httpx.Response(201, json={"id": "thread"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="http://test") as client:
        if agent:
            assert await create_default_agent_thread(client, {}, title_prefix="viewer-security") == "thread"
        else:
            try:
                with pytest.raises(AssertionError, match="default agent response must include its slug"):
                    await create_default_agent_thread(client, {}, title_prefix="viewer-security")
            except pytest.skip.Exception:
                pytest.fail("Malformed success response must fail, not skip")
