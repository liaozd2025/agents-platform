"""只读知识库 MCP、OAuth 协议与浏览器确认入口。"""

import os
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, ValidationError
from mcp.server.auth.provider import RegistrationError
from starlette.responses import JSONResponse
from starlette.routing import Route, compile_path

from server.utils.auth_middleware import get_required_user
from yuxi.services.knowledge_mcp_service import (
    KnowledgeOAuthProvider,
    SCOPE,
    register_knowledge_tools,
)


def build_knowledge_mcp(resource_url: str):
    """装配 MCP，实例地址固定配置，避免信任请求 Host 构造 OAuth 地址。"""
    parts = urlsplit(resource_url)
    if parts.path != "/api/mcp" or parts.query or parts.fragment or parts.username:
        raise ValueError("YUXI_KNOWLEDGE_MCP_URL 必须是入口地址加 /api/mcp")
    development = os.getenv("YUXI_ENV", "development") == "development"
    try:
        private_ip = any(ip_address(parts.hostname) in ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    except ValueError:
        private_ip = False
    explicit_lan = private_ip and os.getenv("YUXI_KNOWLEDGE_MCP_URL", "").strip().rstrip("/") == resource_url
    private_http_exception = os.getenv("YUXI_KNOWLEDGE_MCP_ALLOW_PRIVATE_HTTP", "").lower() == "true"
    if parts.scheme != "https" and not (
        parts.scheme == "http" and (
            (development and parts.hostname in {"localhost", "127.0.0.1", "::1"})
            or (explicit_lan and (development or private_http_exception))
        )
    ):
        raise ValueError("MCP 需要 HTTPS；私有 IP HTTP 需要开发模式或显式内网例外")
    provider = KnowledgeOAuthProvider(resource_url)
    issuer = resource_url
    metadata_url = resource_url + "/metadata"
    server = FastMCP(
        "Yuxi Knowledge",
        token_verifier=provider,
        stateless_http=True,
        json_response=True,
        streamable_http_path="/api/mcp",
        auth=AuthSettings(issuer_url=AnyHttpUrl(issuer), resource_server_url=None, required_scopes=[SCOPE]),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[parts.netloc, "localhost:*", "127.0.0.1:*"],
            allowed_origins=[provider.web_origin],
        ),
    )

    register_knowledge_tools(server)

    router = APIRouter(prefix="/mcp")

    @router.get("/metadata")
    async def resource_metadata():
        """提供客户端授权发现信息。"""
        return {"resource": resource_url, "authorization_servers": [issuer], "scopes_supported": [SCOPE]}

    @router.get("/consent/{request_id}")
    async def consent_info(request_id: str, user=Depends(get_required_user)):
        """展示已登录用户待确认的授权客户端。"""
        try:
            return await provider.consent(request_id)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @router.post("/consent/{request_id}")
    async def consent_approve(request_id: str, user=Depends(get_required_user)):
        """用户显式确认后授权，只使用后端登录身份。"""
        try:
            return await provider.consent(request_id, str(user.id))
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    authorization_handler = AuthorizationHandler(provider)
    token_handler = TokenHandler(provider, ClientAuthenticator(provider))

    async def authorize(request):
        """复用 SDK 的客户端、回调与 PKCE 校验，并限制请求大小。"""
        if len(await request.body()) > 16384:
            return JSONResponse({"error": "invalid_request"}, status_code=413)
        return await authorization_handler.handle(request)

    async def exchange_token(request):
        """补充 SDK 未校验的 token resource 参数，防止跨资源使用授权。"""
        if len(await request.body()) > 16384:
            return JSONResponse({"error": "invalid_request"}, status_code=413)
        form = await request.form()
        if form.get("resource") != resource_url:
            return JSONResponse({"error": "invalid_target"}, status_code=400, headers={"Cache-Control": "no-store"})
        return await token_handler.handle(request)

    async def register(request):
        """注册仅支持当前只读授权的公开客户端。"""
        if len(await request.body()) > 16384:
            return JSONResponse({"error": "invalid_client_metadata"}, status_code=413)
        try:
            data = await provider.create_client(await request.json())
            return JSONResponse(data, status_code=201, headers={"Cache-Control": "no-store"})
        except RegistrationError as error:
            return JSONResponse({"error": error.error, "error_description": error.error_description}, status_code=400)
        except (ValidationError, ValueError):
            return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)

    async def authorization_metadata(request):
        """准确声明公开 PKCE 客户端和实际提供的授权类型。"""
        return JSONResponse(
            {
                "issuer": issuer,
                "authorization_endpoint": issuer + "/oauth/authorize",
                "token_endpoint": issuer + "/oauth/token",
                "registration_endpoint": issuer + "/oauth/register",
                "revocation_endpoint": issuer + "/oauth/revoke",
                "scopes_supported": [SCOPE],
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "token_endpoint_auth_methods_supported": ["none"],
                "code_challenge_methods_supported": ["S256"],
            }
        )

    async def revoke(request):
        """公开客户端凭 client_id 撤销自身 Token，无需不存在的 client_secret。"""
        if len(await request.body()) > 16384:
            return JSONResponse({"error": "invalid_request"}, status_code=413)
        form = await request.form()
        client = await provider.get_client(str(form.get("client_id", "")))
        if client is None:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        raw_token = form.get("token")
        if not isinstance(raw_token, str) or not raw_token:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        token = await provider.load_access_token(raw_token)
        if token and token.client_id == client.client_id:
            await provider.revoke_token(token)
        return JSONResponse({}, headers={"Cache-Control": "no-store"})

    oauth_routes = [
        Route("/authorize", authorize, methods=["GET", "POST"]),
        Route("/token", exchange_token, methods=["POST"]),
        Route("/register", register, methods=["POST"]),
        Route("/revoke", revoke, methods=["POST"]),
        Route("/.well-known/oauth-authorization-server", authorization_metadata, methods=["GET"]),
    ]
    for route in oauth_routes:
        route.path = "/api/mcp/oauth" + route.path
        route.path_regex, route.path_format, route.param_convertors = compile_path(route.path)
    oauth_routes.append(Route("/.well-known/oauth-authorization-server/api/mcp", authorization_metadata))
    async def protected_resource_metadata(request):
        """提供按资源路径发现的标准元数据入口。"""
        return JSONResponse(await resource_metadata())

    oauth_routes.append(Route("/.well-known/oauth-protected-resource/api/mcp", protected_resource_metadata))
    http_app = server.streamable_http_app()

    async def authenticated_app(scope, receive, send):
        """为未认证响应提供 API 前缀下可访问的资源元数据地址。"""

        async def send_with_metadata(message):
            if message["type"] == "http.response.start" and message["status"] == 401:
                headers = [(k, v) for k, v in message["headers"] if k.lower() != b"www-authenticate"]
                headers.append(
                    (b"www-authenticate", f'Bearer resource_metadata="{metadata_url}", scope="{SCOPE}"'.encode())
                )
                message = {**message, "headers": headers}
            await send(message)

        await http_app(scope, receive, send_with_metadata)

    return server, provider, router, oauth_routes, authenticated_app


def configured_knowledge_mcp_url():
    """生产需要显式入口，传输安全由装配边界统一校验。"""
    value = os.getenv("YUXI_KNOWLEDGE_MCP_URL", "").strip()
    if value:
        return value.rstrip("/")
    if os.getenv("YUXI_ENV", "development") not in {"prod", "production"}:
        return "http://localhost:5173/api/mcp"
    return None
