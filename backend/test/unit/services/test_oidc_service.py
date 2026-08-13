from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import unquote

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("OPENAI_API_KEY", "dummy")

from yuxi.services import oidc_service
from yuxi.storage.postgres.models_business import User


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
        await conn.run_sync(User.__table__.create)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


async def _create_user(session, uid: str = "alice") -> User:
    user = User(username="alice", uid=uid, password_hash="x", role="user", is_deleted=0)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


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


async def test_oidc_department_uses_configured_claim_even_without_sync_flag(monkeypatch):
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
    user = await _create_user(oidc_session)
    await oidc_service._create_oidc_binding_placeholder(oidc_session, "tenant:user", user)

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

    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "verify_state",
        AsyncMock(return_value={"redirect_path": "/", "nonce": "nonce-1"}),
    )

    async def fake_exchange(cls, code):
        return {"access_token": "token", "id_token": "id-token"}

    async def fake_userinfo(cls, access_token):
        return {"sub": "tenant:user", "preferred_username": "alice"}

    async def fake_log_operation(db, user_id, operation, request=None):
        return None

    monkeypatch.setattr(oidc_service.OIDCUtils, "exchange_code_for_token", classmethod(fake_exchange))
    monkeypatch.setattr(
        oidc_service.OIDCUtils,
        "verify_id_token",
        AsyncMock(return_value={"sub": "tenant:user", "nonce": "nonce-1"}),
        raising=False,
    )
    monkeypatch.setattr(oidc_service.OIDCUtils, "get_userinfo", classmethod(fake_userinfo))
    monkeypatch.setattr(oidc_service.OIDCUtils, "generate_login_code", AsyncMock(return_value="login-code"))
    monkeypatch.setattr(oidc_service, "log_operation", fake_log_operation)

    response = await oidc_service.oidc_callback_handler("dummy-code", "dummy-state", oidc_session)

    assert response.status_code == 302
    assert unquote(response.headers["location"]).startswith("/auth/oidc/callback?code=")


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
