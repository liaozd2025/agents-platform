"""知识库 MCP 的协议输入边界。"""

from unittest.mock import AsyncMock

import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, RegistrationError
from mcp.shared.auth import OAuthClientInformationFull

from server.routers.knowledge_mcp_router import build_knowledge_mcp, configured_knowledge_mcp_url
from yuxi.services.knowledge_mcp_service import KnowledgeOAuthProvider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redirect",
    ["http://example.com/callback", "https://example.com/#fragment", "https://u:p@example.com/cb", "file:///tmp/cb"],
)
async def test_registration_rejects_unsafe_redirect(redirect):
    """不安全回调不得被保存或获得授权。"""
    provider = KnowledgeOAuthProvider("https://kb.example.com/api/mcp")
    provider.save = AsyncMock()
    with pytest.raises(RegistrationError):
        await provider.create_client({"redirect_uris": [redirect], "token_endpoint_auth_method": "none"})
    provider.save.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,resource",
    [
        (["admin"], "https://kb.example.com/api/mcp"),
        (["knowledge:read"], "https://other.example/mcp"),
        (["knowledge:read"], None),
    ],
)
async def test_authorize_rejects_other_scope_or_resource(scope, resource):
    """调用者不能扩张 scope 或把授权转发给其他资源。"""
    provider = KnowledgeOAuthProvider("https://kb.example.com/api/mcp")
    provider.save = AsyncMock()
    client = OAuthClientInformationFull(client_id="test", redirect_uris=["http://localhost:1234/cb"])
    params = AuthorizationParams(
        state="s",
        scopes=scope,
        resource=resource,
        code_challenge="x",
        redirect_uri="http://localhost:1234/cb",
        redirect_uri_provided_explicitly=True,
    )
    with pytest.raises(AuthorizeError):
        await provider.authorize(client, params)
    provider.save.assert_not_called()


@pytest.mark.parametrize(
    "url", ["http://192.168.1.10/api/mcp", "https://example.com/wrong", "https://u:p@example.com/api/mcp"]
)
def test_public_endpoint_requires_trusted_canonical_url(url, monkeypatch):
    """非本机 HTTP 与非规范路径不能作为公开授权入口。"""
    monkeypatch.delenv("YUXI_KNOWLEDGE_MCP_URL", raising=False)
    with pytest.raises(ValueError):
        build_knowledge_mcp(url)


@pytest.mark.parametrize("environment,host,allowed", [("development", "192.168.1.10", True), ("production", "192.168.1.10", False), ("development", "8.8.8.8", False), ("production", "localhost", False)])
def test_http_only_for_explicit_private_development_endpoint(monkeypatch, environment, host, allowed):
    """开发可显式使用内网 HTTP，生产和公网 HTTP 仍被拒绝。"""
    url = f"http://{host}/api/mcp"
    monkeypatch.setenv("YUXI_ENV", environment)
    monkeypatch.delenv("YUXI_KNOWLEDGE_MCP_ALLOW_PRIVATE_HTTP", raising=False)
    monkeypatch.setenv("YUXI_KNOWLEDGE_MCP_URL", url)
    if allowed:
        build_knowledge_mcp(url)
    else:
        with pytest.raises(ValueError):
            build_knowledge_mcp(url)


@pytest.mark.parametrize("host,flag,allowed", [("192.168.1.112", "true", True), ("192.168.1.112", "false", False), ("8.8.8.8", "true", False), ("example.com", "true", False), ("localhost", "true", False)])
def test_production_private_http_requires_explicit_exception(monkeypatch, host, flag, allowed):
    """内网例外只放行显式私有 IP，不能放行公网或关闭生产模式。"""
    monkeypatch.setenv("YUXI_ENV", "production")
    monkeypatch.setenv("YUXI_KNOWLEDGE_MCP_URL", f"http://{host}/api/mcp")
    monkeypatch.setenv("YUXI_KNOWLEDGE_MCP_ALLOW_PRIVATE_HTTP", flag)
    if allowed:
        build_knowledge_mcp(configured_knowledge_mcp_url())
    else:
        with pytest.raises(ValueError):
            build_knowledge_mcp(configured_knowledge_mcp_url())
