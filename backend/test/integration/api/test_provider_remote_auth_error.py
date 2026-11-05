"""远端模型认证失败通过真实 HTTP 返回可操作且脱敏的提示。"""

import inspect
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Header, HTTPException

from test.integration.support.module_audit import serve_http
from yuxi.api.routers import models
from yuxi.modules.models.tables import ModelProvider

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


async def test_remote_401_keeps_local_login_and_returns_safe_business_error(monkeypatch):
    """供应商拒绝 Key 时返回 502 业务提示，不泄露远端响应或触发本地登录失效。"""
    upstream = FastAPI()

    @upstream.get("/models")
    async def reject_key(authorization: str = Header()):
        """本地远端替身验证鉴权头，并模拟不可信错误内容。"""
        assert authorization == "Bearer fixture-secret"
        raise HTTPException(401, detail="fixture-secret untrusted upstream response")

    async with serve_http(upstream) as remote:
        provider = ModelProvider(
            provider_id="fixture",
            display_name="Fixture",
            base_url=str(remote.base_url),
            models_endpoint="/models",
            capabilities=["chat"],
            api_key="fixture-secret",
        )

        async def read_provider(_db, provider_id):
            """在仓库边界提供配置，保留真实远端 HTTP 客户端。"""
            assert provider_id == "fixture"
            return provider

        monkeypatch.setattr(models, "get_model_provider_by_id", read_provider)
        app = FastAPI()
        app.include_router(models.model_providers, prefix="/api")
        permission_dependency = inspect.signature(models.get_remote_models).parameters["current_user"].default.dependency
        app.dependency_overrides[permission_dependency] = lambda: SimpleNamespace(username="fixture")
        app.dependency_overrides[models.get_db] = lambda: None
        async with serve_http(app) as client:
            response = await client.get("/api/system/model-providers/fixture/remote-models")
        assert response.status_code == 502
        assert response.json() == {
            "detail": {
                "code": "model_provider_auth_failed",
                "message": "远端模型接口认证失败，请检查供应商区域与 API Key 配置",
            }
        }
        assert "fixture-secret" not in response.text
        assert "untrusted upstream" not in response.text
