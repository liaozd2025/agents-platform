"""
Integration tests for evaluation router endpoints.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.routers import knowledge_eval_router

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _upload_test_dataset(test_client, admin_headers: dict[str, str], kb_id: str) -> tuple[str, str]:
    dataset_name = f"pytest_dataset_{uuid.uuid4().hex[:8]}"
    line = '{"query":"什么是单元测试？","gold_answer":"用于验证代码行为的自动化测试"}\n'

    response = await test_client.post(
        f"/api/evaluation/databases/{kb_id}/datasets/upload",
        data={"name": dataset_name, "description": "pytest dataset for download"},
        files={"file": ("pytest_dataset.jsonl", line.encode("utf-8"), "application/x-ndjson")},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text

    payload = response.json()
    assert payload.get("message") == "success"
    dataset_id = payload.get("data", {}).get("dataset_id")
    assert dataset_id
    return dataset_id, line


async def test_download_dataset_requires_admin(test_client, standard_user):
    response = await test_client.get(
        "/api/evaluation/datasets/dataset_fake/download",
        headers=standard_user["headers"],
    )
    assert response.status_code == 403


async def test_admin_can_download_dataset(test_client, admin_headers, knowledge_database):
    dataset_id, expected_line = await _upload_test_dataset(test_client, admin_headers, knowledge_database["kb_id"])

    response = await test_client.get(
        f"/api/evaluation/datasets/{dataset_id}/download",
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert "application/x-ndjson" in response.headers.get("content-type", "")
    assert "attachment" in response.headers.get("content-disposition", "").lower()

    content = response.content.decode("utf-8")
    assert expected_line.strip() in content


async def test_download_dataset_not_found(test_client, admin_headers):
    response = await test_client.get(
        f"/api/evaluation/datasets/dataset_not_found_{uuid.uuid4().hex[:8]}/download",
        headers=admin_headers,
    )
    assert response.status_code == 404, response.text


async def test_run_result_filter_contract_over_http(monkeypatch):
    """HTTP 查询参数必须在依赖边界后按新旧协议校验并传给服务。"""

    captured = []

    class ServiceStub:
        async def get_run_results(self, kb_id, run_id, **kwargs):
            captured.append({"kb_id": kb_id, "run_id": run_id, **kwargs})
            return {"items": []}

    async def allow_database_read():
        return object()

    monkeypatch.setattr(knowledge_eval_router, "EvaluationService", ServiceStub)
    app = FastAPI()
    app.include_router(knowledge_eval_router.evaluation, prefix="/api")
    app.dependency_overrides[knowledge_eval_router.require_evaluation_database_read] = allow_database_read

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unknown = await client.get(
            "/api/evaluation/databases/kb-test/runs/run-test",
            params={"result_filter": "unknown"},
        )
        mixed = await client.get(
            "/api/evaluation/databases/kb-test/runs/run-test",
            params={"result_filter": "all", "error_only": "true"},
        )
        mixed_false = await client.get(
            "/api/evaluation/databases/kb-test/runs/run-test",
            params={"result_filter": "all", "error_only": "false"},
        )
        legacy = await client.get(
            "/api/evaluation/databases/kb-test/runs/run-test",
            params={"error_only": "true"},
        )
        current = await client.get(
            "/api/evaluation/databases/kb-test/runs/run-test",
            params={"page": 2, "page_size": 5, "result_filter": "errors_or_low_recall"},
        )

    assert unknown.status_code == 400
    assert unknown.json()["detail"] == "无效的评估结果筛选条件"
    assert mixed.status_code == 400
    assert mixed.json()["detail"] == "不能同时使用 result_filter 和 error_only"
    assert mixed_false.status_code == 400
    assert mixed_false.json()["detail"] == "不能同时使用 result_filter 和 error_only"
    assert legacy.status_code == 200
    assert current.status_code == 200
    assert captured == [
        {
            "kb_id": "kb-test",
            "run_id": "run-test",
            "page": 1,
            "page_size": 20,
            "result_filter": "legacy_errors",
        },
        {
            "kb_id": "kb-test",
            "run_id": "run-test",
            "page": 2,
            "page_size": 5,
            "result_filter": "errors_or_low_recall",
        },
    ]
