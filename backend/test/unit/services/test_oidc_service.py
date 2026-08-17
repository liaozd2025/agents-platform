from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, unquote, urlparse

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("OPENAI_API_KEY", "dummy")

from yuxi.services import oidc_service, user_identity_service
from yuxi.storage.postgres.models_business import (
    GROUP_NODE_TYPE,
    ROOT_DEPARTMENT_ID,
    Base,
    Department,
    Role,
    User,
    UserRoleAssignment,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class FakeRedis:
    """记录 OIDC 临时值及 TTL，模拟 Redis 的一次性消费接口。"""

    def __init__(self):
        self.values = {}
        self.ttls = {}

    async def set(self, key, value, *, ex):
        self.values[key] = value
        self.ttls[key] = ex

    async def getdel(self, key):
        return self.values.pop(key, None)


@pytest.fixture
def oidc_signing_material():
    """启动本地 JWKS 服务并返回用于签发测试令牌的密钥。"""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
    jwks = {"keys": [jwk]}

    class JWKSHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            response_body = json.dumps(jwks).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), JWKSHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield {
        "private_key": private_key,
        "kid": "test-key",
        "issuer": f"http://127.0.0.1:{server.server_port}",
        "jwks_uri": f"http://127.0.0.1:{server.server_port}/jwks",
        "replace_signing_key": lambda key, kid: jwks.update(
            {
                "keys": [
                    {
                        **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
                        "kid": kid,
                        "use": "sig",
                        "alg": "RS256",
                    }
                ]
            }
        ),
    }

    server.shutdown()
    thread.join()


def issue_id_token(signing_material, **claim_overrides):
    """签发包含 OIDC 必需 claim 的测试 id_token。"""
    now = int(time.time())
    claims = {
        "iss": signing_material["issuer"],
        "aud": "yuxi-client",
        "sub": "oa-user-1",
        "iat": now,
        "exp": now + 300,
        "nonce": "nonce-1",
    }
    claims.update(claim_overrides)
    return jwt.encode(
        claims,
        signing_material["private_key"],
        algorithm="RS256",
        headers={"kid": signing_material["kid"]},
    )


@pytest_asyncio.fixture
async def oidc_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            Department(
                id=ROOT_DEPARTMENT_ID,
                name="集团",
                parent_id=None,
                node_type=GROUP_NODE_TYPE,
                path=f"/{ROOT_DEPARTMENT_ID}/",
            )
        )
        session.add(
            Role(
                code="user",
                name="普通用户",
                is_builtin=True,
                is_active=True,
                default_scope_type="self",
            )
        )
        await session.commit()
        yield session

    await engine.dispose()


async def _create_user(session, uid: str = "alice") -> User:
    role = await session.scalar(select(Role).where(Role.code == "user"))
    user = User(username="alice", uid=uid, password_hash="x", is_deleted=0)
    session.add(user)
    session.add(UserRoleAssignment(user=user, role=role, scope_mode="inherit"))
    await session.commit()
    await session.refresh(user)
    return user


async def test_create_oidc_user_always_uses_builtin_user_role(monkeypatch):
    """OIDC 首次登录不能通过外部配置自动获得管理角色。"""

    captured = {}

    class _UserRepository:
        """记录 OIDC 新用户创建参数的最小仓库替身。"""

        async def create(self, data):
            """保存创建参数并返回测试用户。"""

            captured.update(data)
            return User(id=9, **data)

    monkeypatch.setattr(oidc_service, "UserRepository", _UserRepository)
    monkeypatch.setattr(oidc_service.oidc_config, "use_raw_username", False)
    monkeypatch.setattr(oidc_service, "build_unique_external_username", AsyncMock(return_value="新用户"))

    await oidc_service.create_oidc_user(
        AsyncMock(),
        {"sub": "new-sub", "name": "新用户", "username": "new-user"},
        ROOT_DEPARTMENT_ID,
    )

    assert "role" not in captured


async def test_resolve_external_department_returns_unique_exact_match(oidc_session):
    """唯一同名 claim 应精确挂载已有组织节点。"""
    department = Department(name="研发部", parent_id=ROOT_DEPARTMENT_ID, path=f"/{ROOT_DEPARTMENT_ID}/2/")
    oidc_session.add(department)
    await oidc_session.commit()

    resolved = await oidc_service.resolve_external_department(oidc_session, "研发部")

    assert resolved.id == department.id


async def test_resolve_external_department_falls_back_to_root_when_no_exact_match(oidc_session, monkeypatch):
    """零命中时应回落集团根且不能解析路径或创建节点。"""
    oidc_session.add(Department(name="研发部", parent_id=ROOT_DEPARTMENT_ID, path=f"/{ROOT_DEPARTMENT_ID}/2/"))
    await oidc_session.commit()
    count_before = await oidc_session.scalar(select(func.count(Department.id)))
    warnings = []
    monkeypatch.setattr(
        user_identity_service,
        "logger",
        SimpleNamespace(info=lambda _: None, warning=warnings.append),
    )

    resolved = await oidc_service.resolve_external_department(oidc_session, "集团/研发部")

    assert resolved.id == ROOT_DEPARTMENT_ID
    assert await oidc_session.scalar(select(func.count(Department.id))) == count_before
    assert len(warnings) == 1


async def test_resolve_external_department_falls_back_to_root_when_name_is_duplicated(oidc_session, monkeypatch):
    """多个同名节点时应回落集团根而不是猜测。"""
    company_a = Department(name="A 公司", parent_id=ROOT_DEPARTMENT_ID, path=f"/{ROOT_DEPARTMENT_ID}/2/")
    company_b = Department(name="B 公司", parent_id=ROOT_DEPARTMENT_ID, path=f"/{ROOT_DEPARTMENT_ID}/3/")
    oidc_session.add_all([company_a, company_b])
    await oidc_session.flush()
    oidc_session.add_all(
        [
            Department(name="财务部", parent_id=company_a.id, path=f"{company_a.path}4/"),
            Department(name="财务部", parent_id=company_b.id, path=f"{company_b.path}5/"),
        ]
    )
    await oidc_session.commit()
    warnings = []
    monkeypatch.setattr(
        user_identity_service,
        "logger",
        SimpleNamespace(info=lambda _: None, warning=warnings.append),
    )

    resolved = await oidc_service.resolve_external_department(oidc_session, "财务部")

    assert resolved.id == ROOT_DEPARTMENT_ID
    assert len(warnings) == 1


async def test_oidc_state_uses_redis_ttl_and_is_consumed_once(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(oidc_service, "get_async_redis_client", AsyncMock(return_value=redis), raising=False)

    state = await oidc_service.OIDCUtils.generate_state("/embed", "nonce-1")
    first = await oidc_service.OIDCUtils.verify_state(state)
    second = await oidc_service.OIDCUtils.verify_state(state)

    assert redis.ttls[f"oidc:state:{state}"] == 300
    assert first == {"redirect_path": "/embed", "nonce": "nonce-1"}
    assert second is None


async def test_oidc_login_code_uses_redis_ttl_and_is_consumed_once(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(oidc_service, "get_async_redis_client", AsyncMock(return_value=redis), raising=False)
    payload = {"access_token": "yuxi-token", "user_id": 1}

    code = await oidc_service.OIDCUtils.generate_login_code(payload)
    first = await oidc_service.OIDCUtils.consume_login_code(code)
    second = await oidc_service.OIDCUtils.consume_login_code(code)

    assert redis.ttls[f"oidc:logincode:{code}"] == 60
    assert first == payload
    assert second is None


async def test_oidc_id_token_is_verified_against_provider_jwks(monkeypatch, oidc_signing_material):
    metadata = oidc_service.OIDCProviderMetadata()
    metadata.issuer = oidc_signing_material["issuer"]
    metadata.jwks_uri = oidc_signing_material["jwks_uri"]
    metadata.id_token_signing_alg_values_supported = ["RS256"]
    monkeypatch.setattr(oidc_service.oidc_config, "client_id", "yuxi-client")
    monkeypatch.setattr(oidc_service.OIDCUtils, "get_metadata", AsyncMock(return_value=metadata))

    claims = await oidc_service.OIDCUtils.verify_id_token(
        issue_id_token(oidc_signing_material),
        "nonce-1",
    )

    assert claims["sub"] == "oa-user-1"


async def test_oidc_id_token_rejects_nonce_mismatch(monkeypatch, oidc_signing_material):
    metadata = oidc_service.OIDCProviderMetadata()
    metadata.issuer = oidc_signing_material["issuer"]
    metadata.jwks_uri = oidc_signing_material["jwks_uri"]
    metadata.id_token_signing_alg_values_supported = ["RS256"]
    monkeypatch.setattr(oidc_service.oidc_config, "client_id", "yuxi-client")
    monkeypatch.setattr(oidc_service.OIDCUtils, "get_metadata", AsyncMock(return_value=metadata))

    with pytest.raises(jwt.InvalidTokenError, match="nonce"):
        await oidc_service.OIDCUtils.verify_id_token(
            issue_id_token(oidc_signing_material),
            "different-nonce",
        )


@pytest.mark.parametrize(
    ("claim_overrides", "error_type"),
    [
        ({"exp": 0}, jwt.ExpiredSignatureError),
        ({"aud": "other-client"}, jwt.InvalidAudienceError),
        ({"iss": "https://attacker.example.test"}, jwt.InvalidIssuerError),
    ],
)
async def test_oidc_id_token_rejects_invalid_standard_claims(
    monkeypatch,
    oidc_signing_material,
    claim_overrides,
    error_type,
):
    metadata = oidc_service.OIDCProviderMetadata()
    metadata.issuer = oidc_signing_material["issuer"]
    metadata.jwks_uri = oidc_signing_material["jwks_uri"]
    metadata.id_token_signing_alg_values_supported = ["RS256"]
    monkeypatch.setattr(oidc_service.oidc_config, "client_id", "yuxi-client")
    monkeypatch.setattr(oidc_service.OIDCUtils, "get_metadata", AsyncMock(return_value=metadata))

    with pytest.raises(error_type):
        await oidc_service.OIDCUtils.verify_id_token(
            issue_id_token(oidc_signing_material, **claim_overrides),
            "nonce-1",
        )


async def test_oidc_id_token_rejects_wrong_signing_key(monkeypatch, oidc_signing_material):
    metadata = oidc_service.OIDCProviderMetadata()
    metadata.issuer = oidc_signing_material["issuer"]
    metadata.jwks_uri = oidc_signing_material["jwks_uri"]
    metadata.id_token_signing_alg_values_supported = ["RS256"]
    monkeypatch.setattr(oidc_service.oidc_config, "client_id", "yuxi-client")
    monkeypatch.setattr(oidc_service.OIDCUtils, "get_metadata", AsyncMock(return_value=metadata))
    attacker_material = {
        **oidc_signing_material,
        "private_key": rsa.generate_private_key(public_exponent=65537, key_size=2048),
    }

    with pytest.raises(jwt.InvalidSignatureError):
        await oidc_service.OIDCUtils.verify_id_token(
            issue_id_token(attacker_material),
            "nonce-1",
        )


@pytest.mark.parametrize("azp", [None, "other-client"])
async def test_oidc_id_token_rejects_invalid_authorized_party(monkeypatch, oidc_signing_material, azp):
    metadata = oidc_service.OIDCProviderMetadata()
    metadata.issuer = oidc_signing_material["issuer"]
    metadata.jwks_uri = oidc_signing_material["jwks_uri"]
    metadata.id_token_signing_alg_values_supported = ["RS256"]
    monkeypatch.setattr(oidc_service.oidc_config, "client_id", "yuxi-client")
    monkeypatch.setattr(oidc_service.OIDCUtils, "get_metadata", AsyncMock(return_value=metadata))
    claims = {"aud": ["yuxi-client", "other-client"]}
    if azp is not None:
        claims["azp"] = azp

    with pytest.raises(jwt.InvalidTokenError, match="authorized party"):
        await oidc_service.OIDCUtils.verify_id_token(
            issue_id_token(oidc_signing_material, **claims),
            "nonce-1",
        )


async def test_oidc_id_token_refreshes_jwks_after_key_rotation(monkeypatch, oidc_signing_material):
    metadata = oidc_service.OIDCProviderMetadata()
    metadata.issuer = oidc_signing_material["issuer"]
    metadata.jwks_uri = oidc_signing_material["jwks_uri"]
    metadata.id_token_signing_alg_values_supported = ["RS256"]
    monkeypatch.setattr(oidc_service.oidc_config, "client_id", "yuxi-client")
    monkeypatch.setattr(oidc_service.OIDCUtils, "get_metadata", AsyncMock(return_value=metadata))
    await oidc_service.OIDCUtils.verify_id_token(issue_id_token(oidc_signing_material), "nonce-1")

    rotated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    oidc_signing_material["replace_signing_key"](rotated_key, "rotated-key")
    rotated_material = {**oidc_signing_material, "private_key": rotated_key, "kid": "rotated-key"}

    claims = await oidc_service.OIDCUtils.verify_id_token(issue_id_token(rotated_material), "nonce-1")

    assert claims["sub"] == "oa-user-1"


async def test_oidc_department_uses_configured_claim_when_sync_enabled(monkeypatch):
    monkeypatch.setattr(oidc_service.oidc_config, "fetch_department_info", True)
    monkeypatch.setattr(oidc_service.oidc_config, "department_claim", "oa_department")

    user_info = oidc_service.OIDCUtils.extract_user_info(
        {"sub": "oa-user-1", "preferred_username": "alice", "oa_department": "研发部"}
    )

    assert user_info["department_name"] == "研发部"


async def test_explicit_oidc_endpoints_require_identity_verification_config():
    config = oidc_service.OIDCConfig(
        enabled=True,
        client_id="yuxi-client",
        client_secret="secret",
        authorization_endpoint="https://oa.example.test/authorize",
        token_endpoint="https://oa.example.test/token",
        userinfo_endpoint="https://oa.example.test/userinfo",
    )

    assert config.is_configured() is False
    assert config.is_token_exchange_configured() is False

    config.issuer_url = "https://oa.example.test"
    config.jwks_uri = "https://oa.example.test/jwks"

    assert config.is_configured() is True
    assert config.is_token_exchange_configured() is True

    config.jwks_uri = "http://attacker.example.test/jwks"

    assert config.is_configured() is False
    assert config.is_token_exchange_configured() is False


async def test_local_http_oidc_is_development_only(monkeypatch):
    endpoint = "http://host.docker.internal:9001/oidc"

    monkeypatch.setenv("YUXI_ENV", "development")
    assert oidc_service.is_allowed_oidc_endpoint(endpoint, endpoint) is True

    monkeypatch.setenv("YUXI_ENV", "production")
    assert oidc_service.is_allowed_oidc_endpoint(endpoint, endpoint) is False


@pytest.mark.parametrize(
    ("metadata_overrides", "expected_error"),
    [
        ({"issuer": None}, "discovery 响应的 issuer 与配置不一致"),
        ({"issuer": "https://attacker.example.test"}, "discovery 响应的 issuer 与配置不一致"),
        ({"jwks_uri": None}, "discovery 响应缺少 jwks_uri"),
        ({"jwks_uri": "http://127.0.0.1/jwks"}, "discovery 响应包含不安全端点 jwks_uri"),
    ],
)
async def test_oidc_discovery_rejects_incomplete_identity_metadata(
    monkeypatch,
    metadata_overrides,
    expected_error,
):
    response = MagicMock()
    metadata_payload = {
        "issuer": "https://oa.example.test",
        "authorization_endpoint": "https://oa.example.test/authorize",
        "token_endpoint": "https://oa.example.test/token",
        "userinfo_endpoint": "https://oa.example.test/userinfo",
        "jwks_uri": "https://oa.example.test/jwks",
    }
    metadata_payload.update(metadata_overrides)
    response.json.return_value = metadata_payload
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.get = AsyncMock(return_value=response)
    monkeypatch.setattr(oidc_service.httpx, "AsyncClient", lambda: client)
    metadata = oidc_service.OIDCProviderMetadata()

    loaded = await metadata.load("https://oa.example.test")

    assert loaded is False
    assert metadata.last_error == expected_error


async def test_oidc_id_token_rejects_http_jwks_for_https_issuer(monkeypatch, oidc_signing_material):
    metadata = oidc_service.OIDCProviderMetadata()
    metadata.issuer = "https://oa.example.test"
    metadata.jwks_uri = oidc_signing_material["jwks_uri"]
    metadata.id_token_signing_alg_values_supported = ["RS256"]
    monkeypatch.setattr(oidc_service.oidc_config, "client_id", "yuxi-client")
    monkeypatch.setattr(oidc_service.OIDCUtils, "get_metadata", AsyncMock(return_value=metadata))
    signing_material = {**oidc_signing_material, "issuer": metadata.issuer}

    with pytest.raises(jwt.InvalidTokenError, match="jwks_uri must use HTTPS"):
        await oidc_service.OIDCUtils.verify_id_token(issue_id_token(signing_material), "nonce-1")


async def test_oidc_callback_redirect_only_targets_allowed_oa_origin(monkeypatch):
    monkeypatch.setenv(
        "YUXI_EMBED_ALLOWED_ORIGINS",
        "https://oa.example.test https://oa.example.test/path https://user@oa.example.test https://oa.example.test:443",
    )

    allowed = oidc_service.build_oidc_callback_redirect(
        "login-code",
        "https://oa.example.test/yuxi/callback?source=menu",
    )
    denied = oidc_service.build_oidc_callback_redirect(
        "login-code",
        "https://attacker.example.test/callback",
    )

    assert allowed == "https://oa.example.test/yuxi/callback?source=menu&code=login-code"
    assert denied == "/auth/oidc/callback?code=login-code"
    assert oidc_service.get_embed_allowed_origins() == ["https://oa.example.test"]


async def test_find_user_by_oidc_sub_resolves_placeholder_when_sub_contains_colon(oidc_session):
    user = await _create_user(oidc_session)

    await oidc_service._create_oidc_binding_placeholder(oidc_session, "tenant:user", user)

    resolved = await oidc_service.find_user_by_oidc_sub(oidc_session, "tenant:user")

    assert resolved is not None
    assert resolved.id == user.id
    assert resolved.uid == user.uid
    assert resolved.is_deleted == 0


async def test_find_deleted_oidc_user_by_sub_resolves_deleted_target_when_sub_contains_colon(oidc_session):
    user = await _create_user(oidc_session)
    user.is_deleted = 1
    await oidc_session.commit()

    await oidc_service._create_oidc_binding_placeholder(oidc_session, "tenant:user", user)

    resolved = await oidc_service.find_deleted_oidc_user_by_sub(oidc_session, "tenant:user")

    assert resolved is not None
    assert resolved.id == user.id
    assert resolved.uid == user.uid
    assert resolved.is_deleted == 1


async def test_oidc_callback_allows_existing_binding_when_sub_contains_colon(oidc_session, monkeypatch):
    """已有 OIDC 用户登录时也应按本次 claim 更新归属节点。"""
    user = await _create_user(oidc_session)
    await oidc_service._create_oidc_binding_placeholder(oidc_session, "tenant:user", user)
    department = Department(name="研发部", parent_id=ROOT_DEPARTMENT_ID, path=f"/{ROOT_DEPARTMENT_ID}/2/")
    oidc_session.add(department)
    await oidc_session.commit()

    monkeypatch.setattr(oidc_service.oidc_config, "enabled", True)
    monkeypatch.setattr(oidc_service.oidc_config, "client_id", "cid")
    monkeypatch.setattr(oidc_service.oidc_config, "client_secret", "secret")
    monkeypatch.setattr(oidc_service.oidc_config, "issuer_url", "https://example")
    monkeypatch.setattr(oidc_service.oidc_config, "token_endpoint", "https://example/token")
    monkeypatch.setattr(oidc_service.oidc_config, "authorization_endpoint", "https://example/auth")
    monkeypatch.setattr(oidc_service.oidc_config, "userinfo_endpoint", "https://example/userinfo")
    monkeypatch.setattr(oidc_service.oidc_config, "jwks_uri", "https://example/jwks")
    monkeypatch.setattr(oidc_service.oidc_config, "use_raw_username", True)
    monkeypatch.setattr(oidc_service.oidc_config, "auto_create_user", False)
    monkeypatch.setattr(oidc_service.oidc_config, "fetch_department_info", True)

    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "verify_state",
        AsyncMock(return_value={"redirect_path": "/", "nonce": "nonce-1"}),
    )

    redis = FakeRedis()
    monkeypatch.setattr(oidc_service, "get_async_redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "exchange_code_for_token",
        AsyncMock(return_value={"access_token": "token", "id_token": "id-token"}),
    )
    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "verify_id_token",
        AsyncMock(return_value={"sub": "tenant:user", "nonce": "nonce-1"}),
    )
    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "get_userinfo",
        AsyncMock(return_value={"sub": "tenant:user", "preferred_username": "alice", "department": "研发部"}),
    )
    monkeypatch.setattr(oidc_service, "log_operation", AsyncMock())

    response = await oidc_service.oidc_callback_handler("dummy-code", "dummy-state", oidc_session)

    assert response.status_code == 302
    assert unquote(response.headers["location"]).startswith("/auth/oidc/callback?code=")
    exchange_code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    login_payload = await oidc_service.oidc_exchange_code_handler(exchange_code)
    assert "role" not in login_payload
    assert [role["code"] for role in login_payload["roles"]] == ["user"]
    assert login_payload["effective_permissions"] == ["agent:use"]
    await oidc_session.refresh(user)
    assert user.department_id == department.id


async def test_oidc_callback_auto_create_uses_unique_existing_department(oidc_session, monkeypatch):
    """OIDC 自动建用户的主流程应挂载已有唯一节点且不建节点。"""
    department = Department(name="研发部", parent_id=ROOT_DEPARTMENT_ID, path=f"/{ROOT_DEPARTMENT_ID}/2/")
    oidc_session.add(department)
    await oidc_session.commit()
    count_before = await oidc_session.scalar(select(func.count(Department.id)))

    monkeypatch.setattr(oidc_service.oidc_config, "enabled", True)
    monkeypatch.setattr(oidc_service.oidc_config, "client_id", "cid")
    monkeypatch.setattr(oidc_service.oidc_config, "client_secret", "secret")
    monkeypatch.setattr(oidc_service.oidc_config, "issuer_url", "https://example")
    monkeypatch.setattr(oidc_service.oidc_config, "token_endpoint", "https://example/token")
    monkeypatch.setattr(oidc_service.oidc_config, "authorization_endpoint", "https://example/auth")
    monkeypatch.setattr(oidc_service.oidc_config, "userinfo_endpoint", "https://example/userinfo")
    monkeypatch.setattr(oidc_service.oidc_config, "jwks_uri", "https://example/jwks")
    monkeypatch.setattr(oidc_service.oidc_config, "use_raw_username", False)
    monkeypatch.setattr(oidc_service.oidc_config, "auto_create_user", True)
    monkeypatch.setattr(oidc_service.oidc_config, "fetch_department_info", True)
    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "verify_state",
        AsyncMock(return_value={"redirect_path": "/", "nonce": "nonce-1"}),
    )

    async def fake_create_user(db, user_info, department_id):
        """在当前测试会话中按回调传入的组织节点创建用户。"""
        user = User(
            username=user_info["username"],
            uid=f"oidc:{user_info['sub']}",
            password_hash="x",
            department_id=department_id,
            is_deleted=0,
        )
        db.add(user)
        role = await db.scalar(select(Role).where(Role.code == "user"))
        db.add(UserRoleAssignment(user=user, role=role, scope_mode="inherit"))
        await db.commit()
        await db.refresh(user)
        return user

    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "exchange_code_for_token",
        AsyncMock(return_value={"access_token": "token", "id_token": "id-token"}),
    )
    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "verify_id_token",
        AsyncMock(return_value={"sub": "new-user", "nonce": "nonce-1"}),
    )
    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "get_userinfo",
        AsyncMock(return_value={"sub": "new-user", "preferred_username": "new-user", "department": "研发部"}),
    )
    monkeypatch.setattr(oidc_service, "create_oidc_user", fake_create_user)
    monkeypatch.setattr(oidc_service, "log_operation", AsyncMock())
    monkeypatch.setattr(oidc_service, "get_async_redis_client", AsyncMock(return_value=FakeRedis()))

    response = await oidc_service.oidc_callback_handler("dummy-code", "dummy-state", oidc_session)
    created_user = await oidc_session.scalar(select(User).where(User.uid == "oidc:new-user"))

    assert response.status_code == 302
    assert created_user.department_id == department.id
    assert await oidc_session.scalar(select(func.count(Department.id))) == count_before


async def test_oidc_callback_rejects_invalid_id_token_before_userinfo(oidc_session, monkeypatch):
    monkeypatch.setattr(oidc_service.oidc_config, "enabled", True)
    monkeypatch.setattr(oidc_service.oidc_config, "client_id", "cid")
    monkeypatch.setattr(oidc_service.oidc_config, "client_secret", "secret")
    monkeypatch.setattr(oidc_service.oidc_config, "issuer_url", "https://example")
    monkeypatch.setattr(oidc_service.oidc_config, "token_endpoint", "https://example/token")
    monkeypatch.setattr(oidc_service.oidc_config, "authorization_endpoint", "https://example/auth")
    monkeypatch.setattr(oidc_service.oidc_config, "userinfo_endpoint", "https://example/userinfo")
    monkeypatch.setattr(oidc_service.oidc_config, "jwks_uri", "https://example/jwks")
    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "verify_state",
        AsyncMock(return_value={"redirect_path": "/embed", "nonce": "nonce-1"}),
    )
    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "exchange_code_for_token",
        AsyncMock(return_value={"access_token": "token", "id_token": "bad-id-token"}),
    )
    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "verify_id_token",
        AsyncMock(side_effect=jwt.InvalidSignatureError("bad signature")),
        raising=False,
    )
    get_userinfo = AsyncMock()
    monkeypatch.setattr(oidc_service.OIDCUtils, "get_userinfo", get_userinfo)

    response = await oidc_service.oidc_callback_handler("dummy-code", "dummy-state", oidc_session)

    assert response.status_code == 302
    assert "OIDC身份令牌校验失败" in unquote(response.headers["location"])
    get_userinfo.assert_not_awaited()
