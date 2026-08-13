"""OIDC 服务模块。

统一封装 OIDC 配置、工具能力和认证业务处理逻辑
"""

import asyncio
import hashlib
import json
import os
import secrets
import urllib.parse
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from yuxi.repositories.user_repository import UserRepository
from yuxi.services.operation_log_service import log_operation
from yuxi.storage.postgres.models_business import Department, User
from yuxi.storage.redis import get_async_redis_client
from yuxi.utils.auth_utils import AuthUtils
from yuxi.utils.datetime_utils import utc_now_naive
from yuxi.utils.logging_config import logger

# 前端 OIDC 回调路由路径（与 web/src/router/index.js 中的路由保持一致）
FRONTEND_CALLBACK_PATH = "/auth/oidc/callback"
# 登录页路径
FRONTEND_LOGIN_PATH = "/login"
LOCAL_OIDC_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


def is_allowed_oidc_endpoint(url: str, issuer_url: str) -> bool:
    """仅允许 HTTPS OIDC 端点，开发环境可使用本机 HTTP Provider。"""
    try:
        endpoint = urllib.parse.urlsplit(url)
        issuer = urllib.parse.urlsplit(issuer_url)
    except ValueError:
        return False

    if not endpoint.hostname or endpoint.username or endpoint.password:
        return False
    if endpoint.scheme == "https":
        return True

    environment = os.environ.get("YUXI_ENV", "development").strip().lower()
    return (
        environment == "development"
        and endpoint.scheme == "http"
        and endpoint.hostname in LOCAL_OIDC_HOSTS
        and issuer.scheme == "http"
        and issuer.hostname in LOCAL_OIDC_HOSTS
    )


def get_embed_allowed_origins() -> list[str]:
    """读取并规范化允许承载 Yuxi iframe 的 OA origin。"""
    values = os.environ.get("YUXI_EMBED_ALLOWED_ORIGINS", "").replace(",", " ").split()
    origins = []
    for value in values:
        try:
            parsed = urllib.parse.urlsplit(value)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            logger.warning(f"Ignoring invalid YUXI_EMBED_ALLOWED_ORIGINS entry: {value}")
            continue
        origin = f"{parsed.scheme}://{parsed.netloc}"
        uses_default_port = (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username
            or parsed.password
            or uses_default_port
            or value != origin
        ):
            logger.warning(f"Ignoring invalid YUXI_EMBED_ALLOWED_ORIGINS entry: {value}")
            continue
        origins.append(origin)
    return list(dict.fromkeys(origins))


def build_oidc_callback_redirect(exchange_code: str, redirect_path: str) -> str:
    """把登录 code 交给白名单 OA 回调，否则回到 Yuxi 自身回调页。"""
    parsed = urllib.parse.urlsplit(redirect_path)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    if origin in get_embed_allowed_origins():
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query.append(("code", exchange_code))
        return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query), fragment=""))
    return f"{FRONTEND_CALLBACK_PATH}?{urlencode({'code': exchange_code})}"


class OIDCConfig(BaseModel):
    """OIDC 配置模型"""

    enabled: bool = Field(default=False, description="是否启用 OIDC 认证")
    issuer_url: str = Field(default="", description="OIDC Provider 的 issuer URL")
    client_id: str = Field(default="", description="OIDC Client ID")
    client_secret: str = Field(default="", description="OIDC Client Secret")
    redirect_uri: str = Field(default="", description="OIDC 回调 URL")
    authorization_endpoint: str = Field(default="", description="授权端点 URL")
    token_endpoint: str = Field(default="", description="Token 端点 URL")
    userinfo_endpoint: str = Field(default="", description="UserInfo 端点 URL")
    end_session_endpoint: str = Field(default="", description="登出端点 URL")
    jwks_uri: str = Field(default="", description="OIDC Provider 的签名密钥地址")
    provider_name: str = Field(default="OIDC登录", description="认证源名称，显示在登录按钮上的文字")
    scopes: str = Field(default="openid profile email", description="请求的 scope")
    auto_create_user: bool = Field(default=True, description="是否自动创建用户")
    default_role: str = Field(default="user", description="OIDC 用户的默认角色")
    default_department: str = Field(default="OIDC用户", description="OIDC 用户的默认部门")
    username_claim: str = Field(default="preferred_username", description="用户名映射字段")
    email_claim: str = Field(default="email", description="邮箱映射字段")
    name_claim: str = Field(default="name", description="姓名映射字段")
    use_raw_username: bool = Field(default=False, description="是否使用原始用户名（不带oidc前缀）")
    department_claim: str = Field(default="department", description="部门信息映射字段")
    force_prompt_login: bool = Field(default=False, description="是否强制用户重新登录（添加prompt=login参数）")

    @classmethod
    def from_env(cls) -> "OIDCConfig":
        """从环境变量加载配置"""

        def _env(name: str, default: str = "") -> str:
            return os.environ.get(name, default).strip()

        enabled = os.environ.get("OIDC_ENABLED", "false").lower() == "true"

        if not enabled:
            return cls(enabled=False)

        return cls(
            enabled=enabled,
            provider_name=_env("OIDC_PROVIDER_NAME", "OIDC登录"),
            issuer_url=_env("OIDC_ISSUER_URL"),
            client_id=_env("OIDC_CLIENT_ID"),
            client_secret=_env("OIDC_CLIENT_SECRET"),
            redirect_uri=_env("OIDC_REDIRECT_URI"),
            authorization_endpoint=_env("OIDC_AUTHORIZATION_ENDPOINT"),
            token_endpoint=_env("OIDC_TOKEN_ENDPOINT"),
            userinfo_endpoint=_env("OIDC_USERINFO_ENDPOINT"),
            end_session_endpoint=_env("OIDC_END_SESSION_ENDPOINT"),
            jwks_uri=_env("OIDC_JWKS_URI"),
            scopes=_env("OIDC_SCOPES", "openid profile email"),
            auto_create_user=os.environ.get("OIDC_AUTO_CREATE_USER", "true").lower() == "true",
            default_role=_env("OIDC_DEFAULT_ROLE", "user"),
            default_department=_env("OIDC_DEFAULT_DEPARTMENT", "OIDC用户"),
            username_claim=_env("OIDC_USERNAME_CLAIM", "preferred_username"),
            email_claim=_env("OIDC_EMAIL_CLAIM", "email"),
            name_claim=_env("OIDC_NAME_CLAIM", "name"),
            use_raw_username=os.environ.get("OIDC_USE_RAW_USERNAME", "false").lower() == "true",
            department_claim=_env("OIDC_DEPARTMENT_CLAIM", "department"),
            force_prompt_login=os.environ.get("OIDC_FORCE_PROMPT_LOGIN", "true").lower() == "true",
        )

    def is_configured(self) -> bool:
        """检查登录链接生成所需配置是否完整"""
        if not self.enabled:
            return False
        if self.authorization_endpoint:
            required_endpoints = {
                "authorization_endpoint": self.authorization_endpoint,
                "token_endpoint": self.token_endpoint,
                "userinfo_endpoint": self.userinfo_endpoint,
                "jwks_uri": self.jwks_uri,
            }
            return bool(
                self.client_id
                and self.issuer_url
                and all(required_endpoints.values())
                and all(is_allowed_oidc_endpoint(url, self.issuer_url) for url in required_endpoints.values())
                and (
                    not self.end_session_endpoint
                    or is_allowed_oidc_endpoint(self.end_session_endpoint, self.issuer_url)
                )
            )
        return bool(self.client_id and self.issuer_url and is_allowed_oidc_endpoint(self.issuer_url, self.issuer_url))

    def is_token_exchange_configured(self) -> bool:
        """检查授权码换 token 所需配置是否完整"""
        return self.is_configured() and bool(self.client_secret)


oidc_config = OIDCConfig.from_env()


class OIDCProviderMetadata:
    """OIDC Provider 元数据"""

    def __init__(self):
        self.issuer: str | None = None
        self.authorization_endpoint: str | None = None
        self.token_endpoint: str | None = None
        self.userinfo_endpoint: str | None = None
        self.end_session_endpoint: str | None = None
        self.jwks_uri: str | None = None
        self.id_token_signing_alg_values_supported: list[str] = []
        self.last_error: str | None = None

    async def load(self, issuer_url: str) -> bool:
        """从 discovery 端点加载元数据"""
        if not is_allowed_oidc_endpoint(issuer_url, issuer_url):
            self.last_error = "OIDC issuer 必须使用 HTTPS，仅开发环境本机允许 HTTP"
            return False

        discovery_url = f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(discovery_url, timeout=30.0)
                response.raise_for_status()
                metadata = response.json()

            expected_issuer = issuer_url.rstrip("/")
            self.issuer = metadata.get("issuer")
            if self.issuer != expected_issuer:
                self.last_error = "discovery 响应的 issuer 与配置不一致"
                logger.error(f"Failed to load OIDC discovery: {self.last_error}, url={discovery_url}")
                return False
            self.authorization_endpoint = metadata.get("authorization_endpoint")
            self.token_endpoint = metadata.get("token_endpoint")
            self.userinfo_endpoint = metadata.get("userinfo_endpoint")
            self.end_session_endpoint = metadata.get("end_session_endpoint")
            self.jwks_uri = metadata.get("jwks_uri")
            self.id_token_signing_alg_values_supported = metadata.get("id_token_signing_alg_values_supported", [])

            required_fields = {
                "authorization_endpoint": self.authorization_endpoint,
                "token_endpoint": self.token_endpoint,
                "userinfo_endpoint": self.userinfo_endpoint,
                "jwks_uri": self.jwks_uri,
            }
            missing_fields = [name for name, value in required_fields.items() if not value]
            if missing_fields:
                self.last_error = f"discovery 响应缺少 {', '.join(missing_fields)}"
                logger.error(f"Failed to load OIDC discovery: {self.last_error}, url={discovery_url}")
                return False

            insecure_fields = [
                name
                for name, value in required_fields.items()
                if value and not is_allowed_oidc_endpoint(value, expected_issuer)
            ]
            if self.end_session_endpoint and not is_allowed_oidc_endpoint(self.end_session_endpoint, expected_issuer):
                insecure_fields.append("end_session_endpoint")
            if insecure_fields:
                self.last_error = f"discovery 响应包含不安全端点 {', '.join(insecure_fields)}"
                logger.error(f"Failed to load OIDC discovery: {self.last_error}, url={discovery_url}")
                return False

            self.last_error = None
            logger.info(f"OIDC discovery loaded from {discovery_url}")
            return True

        except Exception as e:
            self.last_error = f"{type(e).__name__}: {repr(e)}"
            logger.error(f"Failed to load OIDC discovery: {self.last_error}, url={discovery_url}")
            return False


class OIDCUtils:
    """OIDC 工具类"""

    _metadata: OIDCProviderMetadata | None = None
    _jwks_client: jwt.PyJWKClient | None = None
    _jwks_client_uri: str | None = None
    _state_ttl_seconds = 300
    _login_code_ttl_seconds = 60
    _last_metadata_error: str | None = None

    @classmethod
    async def get_metadata(cls) -> OIDCProviderMetadata | None:
        """获取 OIDC Provider 元数据"""
        if not oidc_config.enabled or not oidc_config.is_configured():
            cls._last_metadata_error = "OIDC 未启用或基础配置不完整"
            return None

        if cls._metadata is None:
            metadata = OIDCProviderMetadata()

            if oidc_config.authorization_endpoint:
                metadata.issuer = oidc_config.issuer_url.rstrip("/") or None
                metadata.authorization_endpoint = oidc_config.authorization_endpoint
                metadata.token_endpoint = oidc_config.token_endpoint
                metadata.userinfo_endpoint = oidc_config.userinfo_endpoint
                metadata.end_session_endpoint = oidc_config.end_session_endpoint
                metadata.jwks_uri = oidc_config.jwks_uri or None
                metadata.id_token_signing_alg_values_supported = ["RS256"]
                cls._last_metadata_error = None
            else:
                success = await metadata.load(oidc_config.issuer_url)
                if not success:
                    cls._last_metadata_error = metadata.last_error or "OIDC discovery 加载失败"
                    return None
            cls._metadata = metadata

        if not cls._metadata.authorization_endpoint:
            cls._last_metadata_error = "OIDC 授权端点不可用"
            return None

        cls._last_metadata_error = None

        return cls._metadata

    @classmethod
    def get_last_metadata_error(cls) -> str | None:
        """获取最近一次 OIDC 元数据加载错误"""
        return cls._last_metadata_error

    @classmethod
    async def generate_state(cls, redirect_path: str, nonce: str) -> str:
        """生成包含重定向路径和 nonce 的一次性 state。"""
        state = secrets.token_urlsafe(32)
        redis = await get_async_redis_client()
        await redis.set(
            f"oidc:state:{state}",
            json.dumps({"redirect_path": redirect_path, "nonce": nonce}),
            ex=cls._state_ttl_seconds,
        )
        return state

    @classmethod
    async def verify_state(cls, state: str) -> dict[str, Any] | None:
        """原子消费 state 并返回本次授权请求上下文。"""
        redis = await get_async_redis_client()
        state_data = await redis.getdel(f"oidc:state:{state}")
        return json.loads(state_data) if state_data else None

    @classmethod
    async def generate_login_code(cls, payload: dict[str, Any]) -> str:
        """生成存入 Redis 的一次性短期登录 code。"""
        code = secrets.token_urlsafe(32)
        redis = await get_async_redis_client()
        await redis.set(f"oidc:logincode:{code}", json.dumps(payload), ex=cls._login_code_ttl_seconds)
        return code

    @classmethod
    async def consume_login_code(cls, code: str) -> dict[str, Any] | None:
        """原子消费一次性短期登录 code。"""
        redis = await get_async_redis_client()
        payload = await redis.getdel(f"oidc:logincode:{code}")
        return json.loads(payload) if payload else None

    @classmethod
    def generate_nonce(cls) -> str:
        """生成 nonce 参数"""
        return secrets.token_urlsafe(32)

    @classmethod
    async def verify_id_token(cls, id_token: str, expected_nonce: str) -> dict[str, Any]:
        """校验 Provider 签发的 id_token 并返回可信 claims。"""
        metadata = await cls.get_metadata()
        if not metadata or not metadata.issuer or not metadata.jwks_uri:
            raise jwt.InvalidTokenError("OIDC metadata missing issuer or jwks_uri")
        if not is_allowed_oidc_endpoint(metadata.jwks_uri, metadata.issuer):
            raise jwt.InvalidTokenError("OIDC jwks_uri must use HTTPS")

        if cls._jwks_client is None or cls._jwks_client_uri != metadata.jwks_uri:
            cls._jwks_client = jwt.PyJWKClient(metadata.jwks_uri, cache_jwk_set=True, lifespan=300)
            cls._jwks_client_uri = metadata.jwks_uri

        signing_key = await asyncio.to_thread(cls._jwks_client.get_signing_key_from_jwt, id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=metadata.id_token_signing_alg_values_supported or ["RS256"],
            audience=oidc_config.client_id,
            issuer=metadata.issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
        )

        audience = claims["aud"]
        authorized_party = claims.get("azp")
        if authorized_party is not None and authorized_party != oidc_config.client_id:
            raise jwt.InvalidTokenError("OIDC authorized party mismatch")
        if isinstance(audience, list) and len(audience) > 1 and authorized_party != oidc_config.client_id:
            raise jwt.InvalidTokenError("OIDC authorized party missing for multiple audiences")

        token_nonce = str(claims.get("nonce", ""))
        if not token_nonce or not secrets.compare_digest(token_nonce, expected_nonce):
            raise jwt.InvalidTokenError("OIDC nonce mismatch")
        return claims

    @classmethod
    async def build_authorization_url(cls, redirect_path: str = "/") -> str | None:
        """构建授权 URL"""
        metadata = await cls.get_metadata()
        if not metadata or not metadata.authorization_endpoint:
            return None

        nonce = cls.generate_nonce()
        state = await cls.generate_state(redirect_path, nonce)

        redirect_uri = oidc_config.redirect_uri
        if not redirect_uri:
            redirect_uri = "/api/auth/oidc/callback"

        params = {
            "client_id": oidc_config.client_id,
            "response_type": "code",
            "scope": oidc_config.scopes,
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": nonce,
        }

        # 如果配置强制登录，添加 prompt=login 参数
        if oidc_config.force_prompt_login:
            params["prompt"] = "login"

        query_string = urllib.parse.urlencode(params)
        return f"{metadata.authorization_endpoint}?{query_string}"

    @classmethod
    async def exchange_code_for_token(cls, code: str) -> dict[str, Any] | None:
        """用授权码交换令牌"""
        metadata = await cls.get_metadata()
        if not metadata or not metadata.token_endpoint:
            return None

        redirect_uri = oidc_config.redirect_uri or "/api/auth/oidc/callback"

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": oidc_config.client_id,
            "client_secret": oidc_config.client_secret,
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    metadata.token_endpoint,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=30.0,
                )
                response.raise_for_status()
                return response.json()

        except Exception as e:
            logger.error(f"Failed to exchange code for token: {e}")
            return None

    @classmethod
    async def get_userinfo(cls, access_token: str) -> dict[str, Any] | None:
        """获取用户信息"""
        metadata = await cls.get_metadata()
        if not metadata or not metadata.userinfo_endpoint:
            return None

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    metadata.userinfo_endpoint,
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=30.0,
                )
                response.raise_for_status()
                return response.json()

        except Exception as e:
            logger.error(f"Failed to get userinfo: {e}")
            return None

    @classmethod
    async def build_logout_url(cls, id_token: str | None = None) -> str | None:
        """构建登出 URL"""
        metadata = await cls.get_metadata()
        if not metadata or not metadata.end_session_endpoint:
            return None

        params = {"client_id": oidc_config.client_id}

        if id_token:
            params["id_token_hint"] = id_token

        if oidc_config.redirect_uri:
            params["post_logout_redirect_uri"] = oidc_config.redirect_uri

        query_string = urllib.parse.urlencode(params)
        return f"{metadata.end_session_endpoint}?{query_string}"

    @classmethod
    def extract_user_info(cls, userinfo: dict[str, Any]) -> dict[str, Any]:
        """从 userinfo 中提取用户信息"""
        sub = userinfo.get("sub", "")

        username = userinfo.get(oidc_config.username_claim, "")
        if not username:
            username = userinfo.get("preferred_username", "")
        if not username:
            username = userinfo.get("email", "").split("@")[0]
        if not username:
            username = sub[:20]

        email = userinfo.get(oidc_config.email_claim, "")
        if not email:
            email = userinfo.get("email", "")

        name = userinfo.get(oidc_config.name_claim, "")
        if not name:
            name = userinfo.get("name", "")
        if not name:
            name = username

        department_name = userinfo.get(oidc_config.department_claim)
        department_description = userinfo.get("department_description") or userinfo.get("department_desc")
        if not department_name:
            logger.warning(
                f"OIDC identity missing department claim '{oidc_config.department_claim}', "
                f"falling back to default department '{oidc_config.default_department}'"
            )

        return {
            "sub": sub,
            "username": username,
            "email": email,
            "name": name,
            "department_name": department_name,
            "department_description": department_description,
            "raw": userinfo,
        }


async def get_or_create_external_department(
    db,
    department_name: str | None = None,
    department_description: str | None = None,
    default_department: str | None = None,
) -> Department | None:
    """获取或创建外部身份所属部门。"""
    processed_dept_name = None
    processed_dept_desc = None

    if department_name:
        processed_dept_name = department_name.strip()
        if len(processed_dept_name) > 50:
            processed_dept_name = processed_dept_name[:50]
        if not processed_dept_name:
            processed_dept_name = None

    if department_description:
        processed_dept_desc = department_description.strip()
        if len(processed_dept_desc) > 255:
            processed_dept_desc = processed_dept_desc[:255]
        if not processed_dept_desc:
            processed_dept_desc = None

    final_dept_name = processed_dept_name or default_department
    if not final_dept_name:
        return None
    final_dept_desc = processed_dept_desc or f"{final_dept_name}部门"

    result = await db.execute(select(Department).filter(Department.name == final_dept_name))
    dept = result.scalar_one_or_none()

    if dept:
        logger.info(f"Using existing department: {final_dept_name}")
        return dept

    dept = Department(
        name=final_dept_name,
        description=final_dept_desc,
    )
    db.add(dept)
    try:
        await db.commit()
        await db.refresh(dept)
        logger.info(f"Created external identity department: {final_dept_name}")
    except IntegrityError:
        await db.rollback()
        result = await db.execute(select(Department).filter(Department.name == final_dept_name))
        dept = result.scalar_one_or_none()

    return dept


async def find_user_by_oidc_sub(db, sub: str) -> User | None:
    """通过 OIDC sub 查找用户"""
    # 方法1: 检查是否有用户的 uid 直接等于 "oidc:{sub}"（标准 OIDC 用户）
    standard_oidc_uid = f"oidc:{sub}"
    # 占位绑定记录会被标记为 is_deleted=1，但我们仍需要查询它们来获取绑定关系
    result = await db.execute(select(User).filter(User.uid == standard_oidc_uid, User.is_deleted == 0))
    user = result.scalar_one_or_none()
    if user:
        return user

    # 绑定占位用户被标记为 is_deleted=1，需要包括deleted来查询
    binding_result = await db.execute(
        select(User)
        .filter(User.uid.like(f"{standard_oidc_uid}:%"), User.is_deleted.in_([0, 1]))
        .order_by(User.id.asc())
    )
    binding_users = list(binding_result.scalars().all())
    if binding_users:
        for placeholder in binding_users:
            target_user_id = _extract_oidc_placeholder_target_user_id(placeholder.uid)
            if target_user_id is None:
                continue
            result = await db.execute(select(User).filter(User.id == target_user_id, User.is_deleted == 0))
            target_user = result.scalar_one_or_none()
            if target_user:
                logger.debug(f"Resolved OIDC binding placeholder {placeholder.uid} to user {target_user_id}")
                return target_user

    return None


async def find_deleted_oidc_user_by_sub(db, sub: str) -> User | None:
    """查找已注销的 OIDC 账户（标准与历史后缀）"""
    oidc_uid = f"oidc:{sub}"

    result = await db.execute(select(User).filter(User.uid == oidc_uid, User.is_deleted == 1))
    deleted_user = result.scalar_one_or_none()
    if deleted_user:
        return deleted_user

    binding_result = await db.execute(
        select(User).filter(User.uid.like(f"{oidc_uid}:%"), User.is_deleted == 1).order_by(User.id.asc())
    )
    binding_users = list(binding_result.scalars().all())
    if binding_users:
        for placeholder in binding_users:
            target_user_id = _extract_oidc_placeholder_target_user_id(placeholder.uid)
            if target_user_id is None:
                continue
            result = await db.execute(select(User).filter(User.id == target_user_id, User.is_deleted == 1))
            target_user = result.scalar_one_or_none()
            if target_user:
                return target_user
    return None


def _extract_oidc_placeholder_target_user_id(uid: str) -> int | None:
    """从占位 uid 中解析真实用户 ID，允许 sub 中包含冒号。"""
    value = str(uid or "").strip()
    if not value.startswith("oidc:"):
        return None

    # 占位格式始终以 `:{target_user_id}` 结尾，因此从右侧拆分即可避免 sub 中的冒号干扰。
    try:
        _prefix, target_user_id = value.rsplit(":", 1)
        return int(target_user_id)
    except ValueError:
        return None


async def _create_oidc_binding_placeholder(db, sub: str, target_user: User) -> None:
    """创建 OIDC sub 绑定占位用户（仅用于记录绑定关系，不用于登录）

    在 use_raw_username 模式下，我们创建一个占位用户格式: oidc:{sub}:{target_user_id},
    占位用户标记为 is_deleted=1（不参与实际登录），仅用于存储绑定关系，
    find_user_by_oidc_sub 查询时会读取该占位记录并解析出绑定的真实用户，
    这样就能在不修改User表结构的前提下，保持绑定关系可验证，防止账号冒用。

    使用传入的同一个 db session，避免跨session一致性问题。
    """
    # 占位用户格式: oidc:{sub}:{target_user_id}，这样find_user_by_oidc_sub可以解析出目标用户ID
    oidc_placeholder_id = f"oidc:{sub}:{target_user.id}"
    # 占位用户标记为 deleted，查询时需要特别包括deleted才能找到
    result = await db.execute(select(User).filter(User.uid == oidc_placeholder_id, User.is_deleted.in_([0, 1])))
    if result.scalar_one_or_none():
        # 占位用户已存在，无需重复创建
        return

    # 创建占位用户：使用随机密码，标记为deleted，不用于实际登录，仅存储绑定关系
    random_password = secrets.token_urlsafe(32)
    password_hash = AuthUtils.hash_password(random_password)

    # username 使用 oidc-binding-{sub_hash} 避免冲突，sub_hash 基于完整 sub 生成
    sub_hash = hashlib.sha256(sub.encode()).hexdigest()[:8]
    username = f"oidc-binding-{sub_hash}"

    placeholder_user = User(
        username=username,
        uid=oidc_placeholder_id,
        phone_number=None,
        avatar=None,
        password_hash=password_hash,
        role=target_user.role,
        department_id=target_user.department_id,
        is_deleted=1,  # 标记为deleted，不参与实际登录
        last_login=utc_now_naive(),
    )

    try:
        db.add(placeholder_user)
        await db.commit()
        logger.info(
            f"Created OIDC binding placeholder (deleted) for sub {sub} -> user {target_user.id} ({target_user.uid})"
        )
    except IntegrityError:
        # 并发创建冲突，回滚后忽略
        await db.rollback()
        logger.info(f"OIDC binding placeholder already exists for sub {sub}")


async def build_unique_external_username(db, preferred_username: str, external_id: str) -> str:
    """为外部身份生成不冲突的显示用户名。"""
    base_username = preferred_username.strip() if preferred_username else ""
    if not base_username:
        base_username = f"external_{external_id[:8]}"

    result = await db.execute(select(User.id).filter(User.username == base_username))
    if result.scalar_one_or_none() is None:
        return base_username

    hash_suffix = hashlib.sha256(external_id.encode()).hexdigest()[:6]
    candidate = f"{base_username}-{hash_suffix}"
    result = await db.execute(select(User.id).filter(User.username == candidate))
    if result.scalar_one_or_none() is None:
        return candidate

    for i in range(2, 100):
        indexed_candidate = f"{candidate}-{i}"
        result = await db.execute(select(User.id).filter(User.username == indexed_candidate))
        if result.scalar_one_or_none() is None:
            return indexed_candidate

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="无法生成可用用户名，请联系管理员",
    )


async def create_oidc_user(db, user_info: dict, department_id: int | None = None) -> User:
    """创建 OIDC 用户"""
    user_repo = UserRepository()

    sub = user_info["sub"]
    preferred_username = user_info["name"] or user_info["username"]

    # 根据配置决定 uid 是否带 oidc 前缀
    if oidc_config.use_raw_username:
        uid = user_info["username"]
        result = await db.execute(select(User).filter(User.uid == uid, User.is_deleted == 0))
        existing_user = result.scalar_one_or_none()
        if existing_user:
            # 用户已存在，必须验证当前sub是否已经绑定到这个用户
            # 如果sub未绑定该用户，不能直接复用，存在账号冒用风险
            user_by_sub = await find_user_by_oidc_sub(db, sub)
            if user_by_sub and user_by_sub.id == existing_user.id:
                # sub 已经正确绑定到该用户，允许返回
                logger.info(f"User with raw uid {uid} already exists and bound to sub {sub}, returning existing user")
                return existing_user
            elif user_by_sub is None:
                # sub 尚未绑定任何用户，可以将sub绑定到这个现有用户
                logger.info(f"Binding new OIDC sub {sub} to existing user with raw uid {uid}")
                await _create_oidc_binding_placeholder(db, sub, existing_user)
                return existing_user
            else:
                # sub 已经绑定到另一个用户，冲突，拒绝创建
                logger.warning(
                    f"Cannot create OIDC user with raw uid {uid}: "
                    f"sub {sub} is already bound to another user {user_by_sub.id}, conflict"
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"UID {uid} 已存在且OIDC标识 {sub} 已绑定到其他账号，请联系管理员处理冲突",
                )
    else:
        uid = f"oidc:{sub}"

    random_password = secrets.token_urlsafe(32)
    password_hash = AuthUtils.hash_password(random_password)

    username = await build_unique_external_username(db, preferred_username, sub)

    for retry_index in range(3):
        try:
            new_user = await user_repo.create(
                {
                    "username": username,
                    "uid": uid,
                    "phone_number": None,
                    "avatar": None,
                    "password_hash": password_hash,
                    "role": oidc_config.default_role,
                    "department_id": department_id,
                    "last_login": utc_now_naive(),
                }
            )
            logger.info(f"Created OIDC user: {new_user.username} ({uid})")

            # use_raw_username 模式下，创建占位用户记录绑定关系
            if oidc_config.use_raw_username:
                await _create_oidc_binding_placeholder(db, sub, new_user)

            return new_user
        except IntegrityError:
            existing_user = await find_user_by_oidc_sub(db, sub)
            if existing_user:
                return existing_user
            username = await build_unique_external_username(db, f"{preferred_username}-{retry_index + 2}", sub)

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="创建 OIDC 用户失败，请重试",
    )


async def restore_deleted_oidc_user(db, deleted_user: User, user_info: dict) -> User:
    """恢复已注销的 OIDC 用户并返回可登录用户"""
    preferred_username = user_info["name"] or user_info["username"]

    deleted_user.is_deleted = 0
    deleted_user.deleted_at = None
    deleted_user.last_login = utc_now_naive()
    deleted_user.phone_number = None
    deleted_user.avatar = None

    if deleted_user.username.startswith("已注销用户-"):
        deleted_user.username = await build_unique_external_username(db, preferred_username, user_info["sub"])

    if deleted_user.password_hash == "DELETED":
        random_password = secrets.token_urlsafe(32)
        deleted_user.password_hash = AuthUtils.hash_password(random_password)

    await db.commit()
    await db.refresh(deleted_user)
    logger.info(f"Restored deleted OIDC user: {deleted_user.username} ({deleted_user.uid})")
    return deleted_user


async def update_oidc_user_login(db, user: User) -> None:
    """更新 OIDC 用户登录时间"""
    user.last_login = utc_now_naive()
    await db.commit()


def _redirect_to_callback(exchange_code: str, redirect_path: str) -> RedirectResponse:
    """成功后把一次性 code 重定向到 Yuxi 或白名单 OA 回调页。"""
    return RedirectResponse(url=build_oidc_callback_redirect(exchange_code, redirect_path), status_code=302)


def _redirect_to_login_with_error(error_message: str) -> RedirectResponse:
    """失败时重定向到登录页并携带错误信息"""
    url = f"{FRONTEND_LOGIN_PATH}?{urlencode({'oidc_error': error_message})}"
    return RedirectResponse(url=url, status_code=302)


async def get_oidc_config_handler():
    """获取 OIDC 配置（供前端使用）"""
    if not oidc_config.enabled or not oidc_config.is_configured():
        return {"enabled": False}

    provider_name = oidc_config.provider_name
    return {"enabled": True, "provider_name": provider_name}


async def oidc_callback_handler(code: str, state: str, db, request: Request | None = None):
    """处理 OIDC 回调 - 重定向到前端 Vue 路由"""

    if not oidc_config.is_token_exchange_configured():
        return _redirect_to_login_with_error("OIDC 配置不完整，请联系管理员")

    state_data = await OIDCUtils.verify_state(state)
    if not state_data:
        return _redirect_to_login_with_error("登录会话已过期，请返回登录页重试")

    token_response = await OIDCUtils.exchange_code_for_token(code)
    if not token_response:
        return _redirect_to_login_with_error("无法获取访问令牌，请返回登录页重试")

    access_token = token_response.get("access_token")
    if not access_token:
        return _redirect_to_login_with_error("无法获取访问令牌，请返回登录页重试")

    id_token = token_response.get("id_token")
    if not id_token:
        return _redirect_to_login_with_error("OIDC身份令牌缺失，请返回登录页重试")

    try:
        id_token_claims = await OIDCUtils.verify_id_token(id_token, state_data["nonce"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        logger.warning(f"OIDC id_token validation failed: {type(exc).__name__}")
        return _redirect_to_login_with_error("OIDC身份令牌校验失败，请返回登录页重试")

    userinfo = await OIDCUtils.get_userinfo(access_token)
    if not userinfo:
        return _redirect_to_login_with_error("无法获取用户信息，请返回登录页重试")

    if userinfo.get("sub") != id_token_claims["sub"]:
        logger.warning("OIDC userinfo subject does not match validated id_token subject")
        return _redirect_to_login_with_error("OIDC用户标识不一致，请返回登录页重试")

    identity_claims = {**id_token_claims, **userinfo, "sub": id_token_claims["sub"]}
    extracted_info = OIDCUtils.extract_user_info(identity_claims)
    sub = extracted_info["sub"]

    if not sub:
        return _redirect_to_login_with_error("无法获取用户标识，请返回登录页重试")

    # 查找用户：总是先通过 sub 查找，保证绑定关系可验证
    user_by_sub = await find_user_by_oidc_sub(db, sub)

    if oidc_config.use_raw_username:
        # 使用原始用户名模式
        username = extracted_info["username"]
        user = None
        if username:
            result = await db.execute(select(User).filter(User.uid == username, User.is_deleted == 0))
            user_by_name = result.scalar_one_or_none()

            if user_by_sub:
                # sub 已经绑定到一个用户
                if user_by_name and user_by_sub.id == user_by_name.id:
                    # sub 绑定的用户就是找到的用户名用户 -> 验证通过
                    user = user_by_name
                    logger.info(f"OIDC user logged in with raw username: {username} (sub: {sub})")
                else:
                    # sub 已经绑定到另一个用户，存在冲突，拒绝登录
                    conflict_name = user_by_sub.username if not user_by_name else user_by_name.username
                    logger.warning(
                        f"OIDC sub {sub} is already bound to a different user, "
                        f"login rejected to prevent account hijacking (conflict: {conflict_name})"
                    )
                    return _redirect_to_login_with_error("OIDC标识已绑定到其他账号，请联系管理员处理绑定冲突")
            else:
                # sub 尚未绑定到任何用户
                if user_by_name:
                    # 用户名存在，且 sub 没有绑定 -> 允许登录，并创建绑定记录
                    # 在不修改表结构的情况下，我们创建一个占位用户 oidc:{sub} 来记录绑定关系
                    # 这个占位用户不会被用来登录，仅用于存储sub -> 用户的绑定关系
                    user = user_by_name
                    logger.info(f"Binding new OIDC sub {sub} to existing user with raw username: {username}")
                    # 创建绑定占位用户（后台静默创建，不影响现有用户）
                    await _create_oidc_binding_placeholder(db, sub, user_by_name)
                else:
                    # 用户名不存在，需要创建新用户
                    if oidc_config.auto_create_user:
                        user = None  # 让后续逻辑创建
                    else:
                        return _redirect_to_login_with_error("用户不存在，请联系管理员开通账号")
        else:
            # 没有获取到 username，回退到按sub查找
            user = user_by_sub
    else:
        # 标准 OIDC 模式，通过 sub 查找
        user = user_by_sub

    if user:
        await update_oidc_user_login(db, user)
        logger.info(f"OIDC user logged in: {user.username}")
    elif oidc_config.auto_create_user:
        deleted_user = await find_deleted_oidc_user_by_sub(db, sub)
        if deleted_user:
            user = await restore_deleted_oidc_user(db, deleted_user, extracted_info)
            logger.info(f"OIDC deleted user restored and logged in: {user.username}")
        else:
            # 从用户信息中获取部门信息
            dept_name = extracted_info.get("department_name")
            dept_desc = extracted_info.get("department_description")
            dept = await get_or_create_external_department(
                db,
                dept_name,
                dept_desc,
                default_department=oidc_config.default_department,
            )
            department_id = dept.id if dept else None
            user = await create_oidc_user(db, extracted_info, department_id)
    else:
        return _redirect_to_login_with_error("用户未注册，请联系管理员开通账号")

    if user.is_deleted:
        return _redirect_to_login_with_error("该账户已注销")

    token_data = {"sub": str(user.id)}
    jwt_token = AuthUtils.create_access_token(token_data)

    await log_operation(db, user.id, "OIDC 登录", request=request)

    department_name = None
    if user.department_id:
        result = await db.execute(select(Department.name).filter(Department.id == user.department_id))
        department_name = result.scalar_one_or_none()

    response_data = {
        "access_token": jwt_token,
        "token_type": "bearer",
        "user_id": user.id,
        "username": user.username,
        "uid": user.uid,
        "phone_number": user.phone_number,
        "avatar": user.avatar,
        "role": user.role,
        "department_id": user.department_id,
        "department_name": department_name,
    }

    exchange_code = await OIDCUtils.generate_login_code(response_data)
    return _redirect_to_callback(exchange_code, state_data["redirect_path"])


async def oidc_exchange_code_handler(code: str) -> dict:
    """用一次性 code 交换登录响应数据"""
    token_data = await OIDCUtils.consume_login_code(code)
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="登录 code 无效或已过期，请重新登录",
        )
    return token_data


async def oidc_login_url_handler(redirect_path: str = "/"):
    """获取 OIDC 登录 URL"""
    if not oidc_config.enabled or not oidc_config.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC 登录暂不可用，请联系管理员",
        )

    login_url = await OIDCUtils.build_authorization_url(redirect_path)
    if not login_url:
        metadata_error = OIDCUtils.get_last_metadata_error()
        if metadata_error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"生成登录链接失败：{metadata_error}",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="生成登录链接失败，请稍后重试或联系管理员",
        )

    return {"login_url": login_url}
