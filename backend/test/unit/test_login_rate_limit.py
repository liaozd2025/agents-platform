"""匿名登录入口的统一限流测试。"""

from fastapi import Request, status
from fastapi.responses import JSONResponse
import pytest

from server.main import LoginRateLimitMiddleware


pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


async def test_oa_token_exchange_is_rate_limited():
    """OA token 交换连续失败后应返回 429。"""
    middleware = LoginRateLimitMiddleware(lambda _scope, _receive, _send: None)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/oa/exchange-token",
            "query_string": b"",
            "headers": [],
            "client": ("192.0.2.15", 1234),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )

    async def reject_login(_request):
        return JSONResponse({"detail": "invalid"}, status_code=status.HTTP_401_UNAUTHORIZED)

    responses = [await middleware.dispatch(request, reject_login) for _ in range(11)]

    assert [response.status_code for response in responses[:10]] == [401] * 10
    assert responses[10].status_code == status.HTTP_429_TOO_MANY_REQUESTS
