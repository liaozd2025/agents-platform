"""访问日志中间件 - 记录请求处理时间"""

import logging
import socket
import time
from collections.abc import Awaitable, Callable
from posixpath import normpath

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

access_logger = logging.getLogger("access_logger")

if not access_logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(fmt="%(asctime)s %(levelname)s: %(message)s", datefmt="%m-%d %H:%M:%S")
    handler.setFormatter(formatter)
    access_logger.addHandler(handler)
    access_logger.setLevel(logging.INFO)
    # 避免传播到根日志记录器，防止重复日志
    access_logger.propagate = False


def is_trusted_web_proxy(peer_ip: str) -> bool:
    """只接受当前 Compose frontend 代理地址发送的真实客户端头。"""
    try:
        return peer_ip in {address[4][0] for address in socket.getaddrinfo("frontend", None)}
    except socket.gaierror:
        return False


def extract_client_ip(request: Request) -> str:
    """以连接地址为准，仅受信代理可提供覆盖写入的 X-Real-IP。"""
    peer_ip = request.client.host if request.client else ""
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip and peer_ip and is_trusted_web_proxy(peer_ip):
        return real_ip
    return peer_ip or "unknown"


class AccessLogMiddleware(BaseHTTPMiddleware):
    """访问日志中间件 - 记录请求处理时间"""

    def __init__(self, app, logger: logging.Logger | None = None):
        super().__init__(app)
        self.logger = logger or access_logger

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        start_time = time.perf_counter()
        client_ip = extract_client_ip(request)

        response = await call_next(request)

        process_time_ms = int((time.perf_counter() - start_time) * 1000)

        query = request.url.query
        # 代理可转发原始多斜杠 URI；分类使用归一化路径，日志仍保留请求路径。
        path = normpath("/" + request.url.path.lstrip("/"))
        if path.startswith(("/api/auth/oidc/", "/auth/oidc/", "/api/system/mcp-servers/oauth/", "/system/mcp-servers/oauth/")):
            query = ""
        self.logger.info(
            f"{client_ip}:{request.client.port if request.client else 'unknown'} - "
            f'"{request.method} {request.url.path}{"?" + query if query else ""} '
            f'HTTP/{request.scope["http_version"]}" '
            f"{response.status_code} - {process_time_ms}ms"
        )

        return response
