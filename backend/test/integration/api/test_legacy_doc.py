"""通过真实 HTTP、MinIO 和解析器验证旧版 Word 文档。"""

import re
import uuid
from pathlib import Path

import pytest

from yuxi.infrastructure.minio.client import get_minio_client
from yuxi.infrastructure.postgres.manager import pg_manager
from yuxi.modules.agents.models.attachments import AgentAttachment
from yuxi.modules.documents.service import parse

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_legacy_doc_upload_parse_and_attachment(test_client, admin_headers, tmp_path):
    """知识库解析 DOC 正文和表格，聊天保存原件但拒绝弹窗预解析。"""
    original = (Path(__file__).resolve().parents[2] / "data/legacy_word.doc").read_bytes()
    filename = f"pytest_legacy_{uuid.uuid4().hex}.doc"
    files = {"file": (filename, original, "application/msword")}
    minio = get_minio_client()

    response = await test_client.get("/api/knowledge/files/supported-types", headers=admin_headers)
    assert response.status_code == 200, response.text
    assert ".doc" in response.json()["file_types"]

    response = await test_client.post("/api/knowledge/files/upload", files=files, headers=admin_headers)
    assert response.status_code == 200, response.text
    uploaded = response.json()
    try:
        assert uploaded["kb_id"] is None
        assert await minio.adownload_file(uploaded["bucket_name"], uploaded["object_name"]) == original
        parsed = await parse(uploaded["file_path"], tmp_path / "parsed", params={"ocr_engine": "disable"})
        assert parsed.markdown_path.is_file()
        markdown = parsed.markdown_path.read_text()
        assert "旧版 Word 解析验收" in markdown
        assert "中文正文 DOC-ROUNDTRIP-2026" in markdown
        assert re.search(r"\|\s*产品\s*\|\s*数量\s*\|", markdown)
        assert re.search(r"\|\s*测试产品\s*\|\s*42\s*\|", markdown)
        assert await minio.adownload_file(uploaded["bucket_name"], uploaded["object_name"]) == original
    finally:
        await minio.adelete_file(uploaded["bucket_name"], uploaded["object_name"])

    response = await test_client.post(
        "/api/knowledge/files/upload",
        files={"file": ("unsupported.ppt", b"unsupported", "application/vnd.ms-powerpoint")},
        headers=admin_headers,
    )
    assert response.status_code == 400, response.text
    assert "Unsupported file type: .ppt" in response.json()["detail"]

    response = await test_client.post("/api/v1/agents/files", files=files, headers=admin_headers)
    assert response.status_code == 201, response.text
    attachment = response.json()
    file_id = attachment["id"]
    bucket = minio.KB_BUCKETS["documents"]
    try:
        private = await test_client.get(f"/api/agent/files/{file_id}", headers=admin_headers)
        assert private.status_code == 200, private.text
        assert private.json()["parse_methods"] == []
        async with pg_manager.get_async_session_context() as db:
            stored = await db.get(AgentAttachment, file_id)
            assert stored is not None and stored.status == "draft"
            object_name = stored.object_name
        assert await minio.adownload_file(bucket, object_name) == original
        response = await test_client.post(f"/api/agent/files/{file_id}/parse", json={}, headers=admin_headers)
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "当前仅支持 PDF 和图片附件解析"
        assert await minio.adownload_file(bucket, object_name) == original
    finally:
        deleted = await test_client.delete(f"/api/v1/agents/files/{file_id}", headers=admin_headers)
        assert deleted.status_code == 200, deleted.text
