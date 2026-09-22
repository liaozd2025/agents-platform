"""通过本地真实 MCP 服务验证发现和调用期间的 HTTP 凭据边界。"""

import asyncio
import socket
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.responses import RedirectResponse

from yuxi.agents.mcp.service import get_mcp_client


@contextmanager
def mcp_server(transport, *, extra_tool=False):
    """启动独立 loopback MCP，记录收到的认证标记及实际请求。"""
    mcp = FastMCP("origin-test", stateless_http=True, json_response=True)

    @mcp.tool()
    def probe() -> str:
        """返回确定性结果以证明完整工具调用成功。"""
        return "MCP_ORIGIN_OK"

    if extra_tool:
        mcp.add_tool(probe, name="other_probe")

    app = mcp.sse_app() if transport == "sse" else mcp.streamable_http_app()
    state = SimpleNamespace(records=[], redirects={})

    async def record_and_redirect(scope, receive, send):
        """记录请求到达事实，按实验状态返回重定向。"""
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            state.records.append(
                {
                    "method": scope["method"],
                    "path": scope["path"],
                    "key_received": headers.get(b"x-vendor-key") == b"synthetic-mcp-key",
                }
            )
            target = state.redirects.get(scope["path"])
            if target:
                await RedirectResponse(target, status_code=307)(scope, receive, send)
                return
        await app(scope, receive, send)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        state.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        state.endpoint = "/sse" if transport == "sse" else "/mcp"
        server = uvicorn.Server(uvicorn.Config(record_and_redirect, log_level="error", lifespan="on"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "本地 MCP 未启动"
            yield state
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive(), "本地 MCP 未停止"


async def connect(url, transport):
    """从产品客户端入口构造真实 MCP 连接。"""
    client = await get_mcp_client(
        {
            "fixture": {
                "transport": transport,
                "url": url,
                "headers": {"X-Vendor-Key": "synthetic-mcp-key"},
                "timeout": 2,
                "sse_read_timeout": 2,
            }
        }
    )
    assert client is not None
    return client


def exception_messages(error):
    """展开 SDK task group 异常以核对拒绝原因。"""
    if isinstance(error, BaseExceptionGroup):
        return " ".join(exception_messages(child) for child in error.exceptions)
    return str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse", "streamable_http"])
@pytest.mark.parametrize("redirect", ["none", "relative", "absolute"])
async def test_same_origin_discovery_and_call_work(transport, redirect):
    """普通连接和同源跳转均保留认证及完整工具结果。"""
    with mcp_server(transport) as source:
        path = source.endpoint
        if redirect != "none":
            path = "/redirect"
            source.redirects[path] = (source.url if redirect == "absolute" else "") + source.endpoint
        client = await connect(source.url + path, transport)
        async with asyncio.timeout(5):
            tools = await client.get_tools()
            assert len(tools) == 1
            result = await tools[0].ainvoke({})
        assert "MCP_ORIGIN_OK" in str(result)
        assert source.records and all(row["key_received"] for row in source.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse", "streamable_http"])
@pytest.mark.parametrize("change", ["hostname", "port"])
@pytest.mark.parametrize("phase", ["discovery", "call"])
async def test_cross_origin_redirect_never_reaches_target(transport, change, phase):
    """发现和已持有工具的调用都在跨源请求到达前拒绝。"""
    with mcp_server(transport) as source, mcp_server(transport) as sink:
        target = sink.url if change == "port" else source.url.replace("127.0.0.1", "localhost")
        client = await connect(source.url + source.endpoint, transport)
        tools = await client.get_tools() if phase == "call" else None
        source.records.clear()
        # SSE 的调用先建立 GET 会话，再向服务提供的 /messages/ POST。
        source.redirects[source.endpoint] = target + sink.endpoint
        error = None
        try:
            async with asyncio.timeout(5):
                if tools:
                    await tools[0].ainvoke({})
                else:
                    await client.get_tools()
        except Exception as exc:
            error = exc
        reached = sink.records if change == "port" else source.records[1:]
        assert not reached, "跨源目标已收到请求，可能包含自定义密钥"
        assert error is not None
        assert "MCP HTTP request must stay on the configured origin" in exception_messages(error)
