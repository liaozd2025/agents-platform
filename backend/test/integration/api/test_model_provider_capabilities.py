"""逐模型能力经过真实 HTTP 保存后仍可回读。"""

import uuid

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """本模块只创建 provider，由 finally 精确删除，不扫描其他测试资源。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """模型配置测试不创建沙箱，不清理并行任务或保留的 UI fixture。"""
    yield


async def test_model_capabilities_survive_http_create_edit_read(test_client, admin_headers):
    provider_id = f"pytest-pi-capabilities-{uuid.uuid4().hex[:8]}"
    model = {
        "id": "capable",
        "type": "chat",
        "source": "manual",
        "context_length": 32768,
        "max_completion_tokens": 4096,
        "input_modalities": ["text", "image"],
        "reasoning": True,
    }
    path = f"/api/system/model-providers/{provider_id}"
    created = False
    try:
        response = await test_client.post(
            "/api/system/model-providers",
            headers=admin_headers,
            json={
                "provider_id": provider_id,
                "display_name": "Pytest PI capabilities",
                "provider_type": "openai",
                "base_url": "http://127.0.0.1:1/v1",
                "capabilities": ["chat"],
                "enabled_models": [model],
                "is_enabled": False,
            },
        )
        assert response.status_code == 200, response.text
        created = True
        fetched = (await test_client.get(path, headers=admin_headers)).json()["data"]["enabled_models"][0]
        for key, value in model.items():
            assert fetched[key] == value
        fetched["reasoning"] = False
        response = await test_client.put(path, headers=admin_headers, json={"enabled_models": [fetched]})
        assert response.status_code == 200, response.text
        persisted = (await test_client.get(path, headers=admin_headers)).json()["data"]["enabled_models"][0]
        assert persisted["reasoning"] is False
        assert persisted["input_modalities"] == ["text", "image"]
        response = await test_client.put(
            path, headers=admin_headers, json={"enabled_models": [{**fetched, "reasoning": "yes"}]}
        )
        assert response.status_code == 400
        assert "reasoning" in response.text
    finally:
        if created:
            response = await test_client.delete(path, headers=admin_headers)
            assert response.status_code == 200, response.text
