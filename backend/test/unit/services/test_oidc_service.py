"""OIDC 配置的生产边界；签名与状态在真实协议 integration 中验证。"""

import pytest

from yuxi.modules.identity.oidc import OIDCConfig


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:pass@example.com",
        "https://example.com/#token",
        "javascript:alert(1)",
        "https://example.com/\nheader",
    ],
)
def test_production_urls_reject_plaintext_credentials_and_ambiguous_urls(monkeypatch, url):
    """生产信任根和发现端点不得携带明文、凭据或歧义控制字符。"""
    monkeypatch.setenv("YUXI_ENV", "production")
    with pytest.raises(ValueError):
        OIDCConfig.validate_url(url)


def test_development_http_is_explicit_and_disabled_config_needs_no_secrets(monkeypatch):
    """开发联调允许 HTTP，关闭 OIDC 的部署不需要配置。"""
    monkeypatch.setenv("YUXI_ENV", "development")
    OIDCConfig.validate_url("http://localhost:39090/realms/test")
    monkeypatch.setenv("OIDC_ENABLED", "false")
    assert not OIDCConfig.from_env().enabled


@pytest.mark.parametrize(
    "overrides",
    [
        {"issuer_url": ""},
        {"redirect_uri": "https://yuxi.example/auth/callback"},
        {"scopes": "profile"},
        {"issuer_url": "https://idp.example?tenant=other"},
    ],
)
def test_incomplete_login_configuration_fails(overrides):
    """启用后必须具备完整信任根、准确回调与 openid scope。"""
    config = OIDCConfig(
        **(
            {
                "enabled": True,
                "issuer_url": "https://idp.example",
                "client_id": "fixture",
                "client_secret": "secret",
                "redirect_uri": "https://yuxi.example/api/auth/oidc/callback",
            }
            | overrides
        )
    )
    with pytest.raises(ValueError):
        config.validate_configuration()


def test_raw_username_mode_keeps_config_but_does_not_prove_identity(monkeypatch):
    """迁移开关允许读取既有绑定，身份归属仍由 issuer+sub 决定。"""
    for name, value in {
        "OIDC_ENABLED": "true",
        "OIDC_USE_RAW_USERNAME": "true",
        "OIDC_ISSUER_URL": "https://idp.example",
        "OIDC_CLIENT_ID": "fixture",
        "OIDC_CLIENT_SECRET": "fixture-secret",
        "OIDC_REDIRECT_URI": "https://app.example/api/auth/oidc/callback",
    }.items():
        monkeypatch.setenv(name, value)
    assert OIDCConfig.from_env().use_raw_username is True
    monkeypatch.setenv("OIDC_DEFAULT_ROLE", "admin")
    with pytest.raises(ValueError, match="普通用户"):
        OIDCConfig.from_env()


def test_oidc_configuration_is_read_at_consumer_boundary(monkeypatch):
    """模块导入后的配置读取仍校验当前环境，不沿用旧单例。"""
    from yuxi.modules.identity.oidc import get_oidc_config

    monkeypatch.setenv("OIDC_ENABLED", "false")
    assert get_oidc_config().enabled is False
    monkeypatch.setenv("OIDC_ENABLED", "true")
    monkeypatch.setenv("OIDC_ISSUER_URL", "")
    with pytest.raises(ValueError, match="完整"):
        get_oidc_config()
