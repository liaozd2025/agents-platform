"""真实 HTTP、PostgreSQL 和 Redis 验证 MCP 登录及跨用户权限。"""

import base64
import hashlib
import json
import os
import secrets
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import delete

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import OperationLog, Role, RolePermission, User, UserRoleAssignment
from yuxi.storage.postgres.models_knowledge import KnowledgeBase
from yuxi.services.knowledge_mcp_service import KnowledgeOAuthProvider
from yuxi.storage.redis import get_async_redis_client
from yuxi.utils.auth_utils import AuthUtils


@pytest.mark.asyncio
async def test_mcp_oauth_and_live_user_visibility():
    """普通用户按自身权限读取，伪造库 ID、PKCE 与撤销后的凭据被拒绝。"""
    pg_manager.initialize()
    suffix = uuid.uuid4().hex
    password = secrets.token_urlsafe(24)
    user_ids = []
    kb_ids = [f"pytest_mcp_{suffix}_a", f"pytest_mcp_{suffix}_b"]
    role_id = None
    async with pg_manager.get_async_session_context() as db:
        role = Role(code=f"pytest_mcp_{suffix}", name="MCP test", default_scope_type="self")
        role.permissions = [RolePermission(permission_key="knowledge_base:read")]
        db.add(role)
        await db.flush()
        role_id = role.id
        for label, kb_id in zip(("a", "b"), kb_ids, strict=True):
            user = User(
                username=f"pytest_mcp_{suffix}_{label}",
                uid=f"pytest_mcp_{suffix}_{label}",
                password_hash=AuthUtils.hash_password(password),
            )
            db.add(user)
            await db.flush()
            user_ids.append(user.id)
            db.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_mode="inherit"))
            db.add(
                KnowledgeBase(
                    kb_id=kb_id,
                    name=kb_id,
                    kb_type="milvus",
                    created_by=user.uid,
                    share_config={
                        "version": 2,
                        "read_scope": {"access_level": "user", "user_uids": [user.uid]},
                        "manage_scope": None,
                    },
                )
            )
        await db.commit()

    try:
        async with httpx.AsyncClient(base_url=os.getenv("TEST_BASE_URL", "http://localhost:5050"), timeout=30) as client:
            resource = (await client.get("/api/mcp/metadata")).json()["resource"]
            client.headers["Host"] = urlsplit(resource).netloc
            discovery = (await client.get("/.well-known/oauth-authorization-server/api/mcp")).json()
            assert discovery["issuer"] == resource
            assert discovery["registration_endpoint"] == resource + "/oauth/register"
            protected = (await client.get("/.well-known/oauth-protected-resource/api/mcp")).json()
            assert protected["authorization_servers"] == [resource]
            oauth = "/api/mcp/oauth"
            response = await client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert response.status_code == 401
            assert "resource_metadata=" in response.headers["www-authenticate"]
            login = await client.post(
                "/api/auth/token", data={"username": f"pytest_mcp_{suffix}_a", "password": password}
            )
            assert login.status_code == 200, login.status_code
            browser_headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            callback = "http://127.0.0.1:49199/callback"
            registration = await client.post(
                oauth + "/register",
                json={
                    "client_name": "MCP integration",
                    "redirect_uris": [callback],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code"],
                    "response_types": ["code"],
                    "scope": "knowledge:read",
                },
            )
            assert registration.status_code == 201, registration.text
            client_id = registration.json()["client_id"]
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            auth_params = {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": callback,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "knowledge:read",
                "resource": resource,
                "state": "test-state",
            }
            authorization = await client.get(oauth + "/authorize", params=auth_params)
            assert authorization.status_code == 302, authorization.text
            request_id = parse_qs(urlsplit(authorization.headers["location"]).query)["request"][0]
            consent_path = "/api/mcp/consent/" + request_id
            assert (await client.post(consent_path)).status_code == 401
            consent = await client.post(consent_path, headers=browser_headers)
            assert consent.status_code == 200, consent.text
            callback_query = parse_qs(urlsplit(consent.json()["redirect_uri"]).query)
            assert callback_query["state"] == ["test-state"]
            token_params = {
                "client_id": client_id,
                "grant_type": "authorization_code",
                "redirect_uri": callback,
                "code": callback_query["code"][0],
                "code_verifier": verifier,
                "resource": resource,
            }
            assert (
                await client.post(oauth + "/token", data={**token_params, "resource": "https://other.example/mcp"})
            ).status_code == 400
            bad_pkce = await client.post(oauth + "/token", data={**token_params, "code_verifier": "wrong"})
            assert bad_pkce.status_code == 400
            assert bad_pkce.json()["error"] == "invalid_grant"
            issued_after = int(time.time())
            redis = await get_async_redis_client()
            client_key = KnowledgeOAuthProvider(resource).key("client", client_id)
            await redis.expire(client_key, 60)
            exchange = await client.post(oauth + "/token", data=token_params)
            assert exchange.status_code == 200, exchange.text
            assert exchange.json()["expires_in"] == 2592000
            token = exchange.json()["access_token"]
            redis = await get_async_redis_client()
            token_key = KnowledgeOAuthProvider(resource).key("token", token)
            stored = json.loads(await redis.get(token_key))
            assert issued_after + 2592000 <= stored["expires_at"] <= int(time.time()) + 2592000
            assert 2592000 - (int(time.time()) - issued_after) - 1 <= await redis.ttl(token_key) <= 2592000
            assert await redis.ttl(client_key) >= await redis.ttl(token_key)
            assert (await client.get("/api/auth/me", headers={"Authorization": "Bearer " + token})).status_code == 401
            assert (
                await client.post(
                    "/api/mcp", headers=browser_headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                )
            ).status_code == 401
            assert (await client.post(oauth + "/token", data=token_params)).status_code == 400
            headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}

            async def rpc(method, params=None):
                """使用真实 MCP JSON-RPC 协议，不在客户端替换服务端授权。"""
                result = await client.post(
                    "/api/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                )
                assert result.status_code == 200, result.text
                return result.json()["result"]

            tools = await rpc("tools/list")
            assert {t["name"] for t in tools["tools"]} == {
                "list_kbs",
                "query_kb",
                "search_file",
                "open_kb_document",
                "find_kb_document",
            }
            listed = await rpc("tools/call", {"name": "list_kbs", "arguments": {}})
            assert not listed.get("isError"), listed
            payload = json.loads(listed["content"][0]["text"])
            visible = {item["kb_id"] for item in payload["databases"]}
            assert kb_ids[0] in visible and kb_ids[1] not in visible
            own = await rpc("tools/call", {"name": "search_file", "arguments": {"kb_id": kb_ids[0]}})
            assert not own.get("isError"), own
            for name, arguments in [
                ("query_kb", {"query": "test"}),
                ("search_file", {}),
                ("open_kb_document", {"file_id": "fake"}),
                ("find_kb_document", {"file_id": "fake", "patterns": ["test"]}),
            ]:
                denied = await rpc("tools/call", {"name": name, "arguments": {"kb_id": kb_ids[1], **arguments}})
                assert denied["isError"] is True
                assert "无权访问" in str(denied)
            async with pg_manager.get_async_session_context() as db:
                await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id == user_ids[0]))
                await db.commit()
            response = await client.post(
                "/api/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
            assert response.status_code == 401
            revocation = await client.post(oauth + "/revoke", data={"client_id": client_id, "token": token})
            assert revocation.status_code == 200, revocation.text
            async with pg_manager.get_async_session_context() as db:
                db.add(UserRoleAssignment(user_id=user_ids[0], role_id=role_id, scope_mode="inherit"))
                await db.commit()
            revoked = await client.post(
                "/api/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
            assert revoked.status_code == 401
    finally:
        async with pg_manager.get_async_session_context() as db:
            await db.execute(delete(KnowledgeBase).where(KnowledgeBase.kb_id.in_(kb_ids)))
            await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(user_ids)))
            await db.execute(delete(OperationLog).where(OperationLog.user_id.in_(user_ids)))
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.execute(delete(RolePermission).where(RolePermission.role_id == role_id))
            await db.execute(delete(Role).where(Role.id == role_id))
            await db.commit()
