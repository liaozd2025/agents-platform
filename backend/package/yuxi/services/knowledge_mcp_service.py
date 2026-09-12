"""远程知识库 MCP 的短期授权与用户权限边界。"""

import hashlib
import json
import secrets
import time
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from yuxi.permissions.authorization import build_authorization_context
from yuxi.repositories.user_repository import UserRepository
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.redis import get_async_redis_client

SCOPE = "knowledge:read"
ACCESS_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60


def register_knowledge_tools(server):
    """将只读知识库用例注册到 MCP，身份来自已认证上下文。"""
    from mcp.server.auth.middleware.auth_context import get_access_token
    from pydantic import Field
    from mcp.types import ToolAnnotations

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    def subject():
        """只从经认证的 MCP 上下文取得身份。"""
        token = get_access_token()
        if not token or not token.subject:
            raise PermissionError("请登录")
        return token.subject

    @server.tool(annotations=read_only)
    async def list_kbs() -> dict:
        """列出当前登录用户有权读取的知识库。"""
        from yuxi.knowledge.runtime import knowledge_base

        user = await require_mcp_user(subject())
        databases = await knowledge_base.get_databases_by_uid(user.uid)
        return {
            "databases": [
                {"kb_id": item.kb_id, "name": item.name, "description": item.description or ""} for item in databases
            ]
        }

    @server.tool(annotations=read_only)
    async def query_kb(kb_id: str, query: str, top_k: int = Field(default=5, ge=1, le=20)) -> dict:
        """在当前用户有权读取的知识库检索证据。"""
        from yuxi.knowledge.runtime import knowledge_base

        await accessible_mcp_database(subject(), kb_id)
        if not query.strip() or len(query) > 10000:
            raise ValueError("查询不能为空且不能超过 10000 字符")
        result = await knowledge_base.retrieve(kb_id, query, final_top_k=top_k)
        return {"kb_id": kb_id, "result": result}

    @server.tool(annotations=read_only)
    async def search_file(
        kb_id: str, query: str = "", offset: int = Field(default=0, ge=0), limit: int = Field(default=50, ge=1, le=100)
    ) -> dict:
        """在授权知识库按文件名查找文件。"""
        from yuxi.knowledge.runtime import knowledge_base

        db = await accessible_mcp_database(subject(), kb_id)
        return await knowledge_base.search_document_files(
            [{"kb_id": kb_id, "name": db.name}],
            query=query,
            offset=offset,
            limit=limit,
            status="all",
            include_is_folder=True,
            include_parent_id=True,
        )

    @server.tool(annotations=read_only)
    async def open_kb_document(
        kb_id: str, file_id: str, offset: int = Field(default=0, ge=0), limit: int = Field(default=200, ge=1, le=1800)
    ) -> dict:
        """读取授权知识库内指定文件的原文行窗口。"""
        from yuxi.knowledge.runtime import knowledge_base

        await accessible_mcp_database(subject(), kb_id)
        return await knowledge_base.open_document(kb_id, file_id, offset=offset, limit=limit)

    @server.tool(annotations=read_only)
    async def find_kb_document(kb_id: str, file_id: str, patterns: list[str]) -> dict:
        """在授权文件内按字面术语定位证据，不执行正则表达式。"""
        from yuxi.knowledge.runtime import knowledge_base

        await accessible_mcp_database(subject(), kb_id)
        if not patterns or len(patterns) > 10 or any(not p or len(p) > 200 for p in patterns):
            raise ValueError("提供 1–10 个术语，每项 1–200 字符")
        return await knowledge_base.find_in_document(
            kb_id, file_id, patterns, use_regex=False, max_windows=5, window_size=80
        )


async def require_mcp_user(subject: str):
    """每次请求从 PostgreSQL 重新确认用户状态与知识库功能权限。"""
    async with pg_manager.get_async_session_context() as db:
        user = await UserRepository().get_by_id_with_db(db, int(subject))
        if not user or user.is_deleted or user.is_login_locked():
            raise PermissionError("用户不可用")
        auth = build_authorization_context(user)
        if not any(auth.has_permission(p) for p in ("knowledge_base:read", "knowledge_base:manage")):
            raise PermissionError("缺少知识库读取权限")
        return user


async def accessible_mcp_database(subject: str, kb_id: str):
    """目标库必须属于当前用户可读范围，不接受客户端指定身份。"""
    from yuxi.knowledge.runtime import knowledge_base

    user = await require_mcp_user(subject)
    database = await knowledge_base.get_accessible_database_info_by_uid(user.uid, kb_id)
    if not database:
        raise PermissionError("知识库不存在或无权访问")
    return database


class KnowledgeOAuthProvider:
    """使用 Redis 保存短期授权；持久用户与资源权限仍由 PostgreSQL 拥有。"""

    def __init__(self, resource_url: str):
        self.resource_url = resource_url
        parts = urlsplit(resource_url)
        self.web_origin = f"{parts.scheme}://{parts.netloc}"
        self.namespace = "knowledge-mcp:" + hashlib.sha256(resource_url.encode()).hexdigest()[:16] + ":"

    def key(self, kind: str, value: str) -> str:
        """仅用摘要索引秘密，避免凭据出现在 Redis key。"""
        return self.namespace + kind + ":" + hashlib.sha256(value.encode()).hexdigest()

    async def read(self, kind: str, value: str):
        """读取未过期的协议状态。"""
        redis = await get_async_redis_client()
        raw = await redis.get(self.key(kind, value))
        return json.loads(raw) if raw else None

    async def save(self, kind: str, value: str, data: dict, ttl: int):
        """保存有明确有效期的协议状态。"""
        redis = await get_async_redis_client()
        await redis.set(self.key(kind, value), json.dumps(data), ex=ttl)

    async def get_client(self, client_id):
        """读取动态注册的客户端。"""
        data = await self.read("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info):
        """仅允许公开 PKCE 客户端与 HTTPS 或本机回调。"""
        if client_info.token_endpoint_auth_method != "none":
            raise RegistrationError("invalid_client_metadata", "使用公开 PKCE 客户端")
        for uri in client_info.redirect_uris or []:
            parts = urlsplit(str(uri))
            local = parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}
            if not (local or parts.scheme == "https") or parts.fragment or parts.username or parts.password:
                raise RegistrationError("invalid_redirect_uri", "回调必须为 HTTPS 或本机 HTTP")
        if not client_info.redirect_uris:
            raise RegistrationError("invalid_redirect_uri", "缺少回调地址")
        client_info.grant_types = ["authorization_code"]
        await self.save("client", client_info.client_id, client_info.model_dump(mode="json"), 86400 * 30)

    async def create_client(self, data: dict):
        """注册公开客户端，精确返回本服务支持的授权类型。"""
        metadata = OAuthClientMetadata.model_validate(data)
        if metadata.scope and set(metadata.scope.split()) != {SCOPE}:
            raise RegistrationError("invalid_client_metadata", "仅支持 knowledge:read")
        if "authorization_code" not in metadata.grant_types or metadata.response_types != ["code"]:
            raise RegistrationError("invalid_client_metadata", "仅支持授权码流程")
        metadata.token_endpoint_auth_method = metadata.token_endpoint_auth_method or "none"
        metadata.scope = SCOPE
        client = OAuthClientInformationFull(
            client_id=secrets.token_urlsafe(24), client_id_issued_at=int(time.time()), **metadata.model_dump()
        )
        await self.register_client(client)
        return client.model_dump(mode="json", exclude_none=True)

    async def authorize(self, client, params: AuthorizationParams):
        """绑定客户端、PKCE、回调和资源后交给平台登录页确认。"""
        if params.resource != self.resource_url:
            raise AuthorizeError("invalid_request", "resource 必须等于 MCP 地址")
        if params.scopes and params.scopes != [SCOPE]:
            raise AuthorizeError("invalid_scope", "仅支持 knowledge:read")
        request_id = secrets.token_urlsafe(32)
        await self.save(
            "pending",
            request_id,
            {
                "client_id": client.client_id,
                "client_name": client.client_name or "MCP 客户端",
                "params": params.model_dump(mode="json"),
            },
            600,
        )
        return self.web_origin + "/auth/mcp/authorize?request=" + request_id

    async def consent(self, request_id: str, subject: str | None = None):
        """展示已验证请求；登录用户确认后生成短期授权码。"""
        pending = await self.read("pending", request_id)
        if not pending:
            raise ValueError("授权请求不存在或已过期，请重新连接 MCP")
        params = AuthorizationParams.model_validate(pending["params"])
        if subject is None:
            return {"client_name": pending["client_name"], "redirect_uri": str(params.redirect_uri), "scope": SCOPE}
        await require_mcp_user(subject)
        code = secrets.token_urlsafe(32)
        auth_code = AuthorizationCode(
            code=code,
            scopes=[SCOPE],
            expires_at=time.time() + 120,
            client_id=pending["client_id"],
            subject=subject,
            **params.model_dump(exclude={"state", "scopes"}),
        )
        redis = await get_async_redis_client()
        # 原子转换 pending，避免重复确认绑定到另一个账号。
        result = await redis.eval(
            "if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end "
            "redis.call('SET', KEYS[2], ARGV[1], 'EX', 120) "
            "redis.call('DEL', KEYS[1]) return 1",
            2,
            self.key("pending", request_id),
            self.key("code", code),
            auth_code.model_dump_json(exclude={"code"}),
        )
        if not result:
            raise ValueError("授权请求已处理，请重新连接 MCP")
        return {"redirect_uri": construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)}

    async def load_authorization_code(self, client, authorization_code):
        """读取授权码，PKCE 与回调验证由 MCP SDK 执行。"""
        data = await self.read("code", authorization_code)
        return AuthorizationCode.model_validate({**data, "code": authorization_code}) if data else None

    async def exchange_authorization_code(self, client, authorization_code):
        """原子签发一次性授权码对应的短期只读凭据。"""
        try:
            await require_mcp_user(authorization_code.subject)
        except PermissionError as error:
            raise TokenError("invalid_grant", "授权用户已失去知识库读取权限") from error
        token = secrets.token_urlsafe(48)
        access = AccessToken(
            token="",
            client_id=client.client_id,
            scopes=[SCOPE],
            expires_at=int(time.time()) + ACCESS_TOKEN_TTL_SECONDS,
            resource=self.resource_url,
            subject=authorization_code.subject,
        )
        redis = await get_async_redis_client()
        result = await redis.eval(
            "if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end "
            "if redis.call('EXISTS', KEYS[3]) == 0 then return 0 end "
            "redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2]) "
            "redis.call('EXPIRE', KEYS[3], ARGV[2]) "
            "redis.call('DEL', KEYS[1]) return 1",
            3,
            self.key("code", authorization_code.code),
            self.key("token", token),
            self.key("client", client.client_id),
            access.model_dump_json(),
            ACCESS_TOKEN_TTL_SECONDS,
        )
        if not result:
            raise TokenError("invalid_grant", "授权码已使用或客户端已过期")
        return OAuthToken(access_token=token, token_type="Bearer", expires_in=ACCESS_TOKEN_TTL_SECONDS, scope=SCOPE)

    async def load_access_token(self, token):
        """读取本实例凭据，允许失去业务权限的客户端继续撤销授权。"""
        data = await self.read("token", token)
        if not data or data.get("resource") != self.resource_url or data.get("expires_at", 0) <= time.time():
            return None
        return AccessToken.model_validate({**data, "token": token})

    async def verify_token(self, token):
        """资源请求必须同时满足凭据有效与用户当前权限。"""
        access = await self.load_access_token(token)
        if access is None:
            return None
        try:
            await require_mcp_user(access.subject)
        except PermissionError:
            return None
        return access

    async def load_refresh_token(self, client, refresh_token):
        """短期授权到期后重新浏览器登录。"""
        return None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        """拒绝未提供的长期刷新能力。"""
        raise TokenError("unsupported_grant_type", "请重新登录")

    async def revoke_token(self, token):
        """撤销当前 MCP 凭据，不影响平台登录。"""
        redis = await get_async_redis_client()
        await redis.delete(self.key("token", token.token))
