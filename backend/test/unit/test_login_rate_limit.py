"""匿名登录入口的统一限流测试。"""

import pytest
from fastapi import Request, status
from fastapi.responses import JSONResponse

from yuxi.api import main
from yuxi.api.middleware import access_log

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest.mark.parametrize("path", ["/api/auth/token", "/api/auth/oa/exchange-token", "/api/auth/oa/exchange-account"])
async def test_oa_token_exchange_is_rate_limited(monkeypatch: pytest.MonkeyPatch, path):
    """OA token 交换不能通过伪造转发链绕过限流。"""
    monkeypatch.setattr(access_log, "is_trusted_web_proxy", lambda _peer_ip: True)
    main._login_attempts.clear()
    middleware = main.LoginRateLimitMiddleware(lambda _scope, _receive, _send: None)

    async def reject_login(_request):
        return JSONResponse({"detail": "invalid"}, status_code=status.HTTP_401_UNAUTHORIZED)

    responses = []
    for attempt in range(11):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "query_string": b"",
                "headers": [
                    (b"x-forwarded-for", f"203.0.113.{attempt}".encode()),
                    (b"x-real-ip", b"192.0.2.15"),
                ],
                "client": ("172.18.0.2", 1234),
                "server": ("testserver", 80),
                "scheme": "http",
            }
        )
        responses.append(await middleware.dispatch(request, reject_login))

    assert [response.status_code for response in responses[:10]] == [401] * 10
    assert responses[10].status_code == status.HTTP_429_TOO_MANY_REQUESTS


@pytest.mark.parametrize("path", ["/api/auth/token", "/api/auth/oa/exchange-token", "/api/auth/oa/exchange-account"])
async def test_direct_api_access_ignores_spoofed_real_ip(monkeypatch: pytest.MonkeyPatch, path):
    """直连 API 时必须按连接地址限流，不能信任客户端请求头。"""
    monkeypatch.setattr(access_log, "is_trusted_web_proxy", lambda _peer_ip: False)
    main._login_attempts.clear()
    middleware = main.LoginRateLimitMiddleware(lambda _scope, _receive, _send: None)

    async def reject_login(_request):
        return JSONResponse({"detail": "invalid"}, status_code=status.HTTP_401_UNAUTHORIZED)

    responses = []
    for attempt in range(11):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "query_string": b"",
                "headers": [(b"x-real-ip", f"203.0.113.{attempt}".encode())],
                "client": ("198.51.100.7", 1234),
                "server": ("testserver", 80),
                "scheme": "http",
            }
        )
        responses.append(await middleware.dispatch(request, reject_login))

    assert responses[10].status_code == status.HTTP_429_TOO_MANY_REQUESTS


async def test_only_resolved_frontend_addresses_are_trusted(monkeypatch):
    """未知连接或代理DNS解析失败不能启用请求头信任。"""
    import socket

    monkeypatch.setattr(access_log.socket, "getaddrinfo", lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.18.0.2", 0))])
    assert access_log.is_trusted_web_proxy("172.18.0.2") is True
    assert access_log.is_trusted_web_proxy("198.51.100.7") is False

    def unavailable(*args):
        raise socket.gaierror("no frontend address")

    monkeypatch.setattr(access_log.socket, "getaddrinfo", unavailable)
    assert access_log.is_trusted_web_proxy("172.18.0.2") is False
