"""通过真实 HTTP、MinIO 和解析器验证旧版 Word 文档。"""

import re
import uuid
from pathlib import Path

import pytest
from yuxi.services.ocr_service import parse_document
from yuxi.storage.minio.client import get_minio_client

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_legacy_doc_upload_parse_and_attachment(test_client, admin_headers):
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
        parsed = await parse_document(uploaded["file_path"])
        response = await test_client.post(
            "/api/knowledge/files/markdown", files=files, headers=admin_headers, timeout=180
        )
        assert response.status_code == 200, response.text
        assert response.json()["message"] == "success"
        for markdown in (parsed, response.json()["markdown_content"]):
            assert "旧版 Word 解析验收" in markdown
            assert "中文正文 DOC-ROUNDTRIP-2026" in markdown
            assert re.search(r"\|\s*产品\s*\|\s*数量\s*\|", markdown)
            assert re.search(r"\|\s*测试产品\s*\|\s*42\s*\|", markdown)
    finally:
        await minio.adelete_file(uploaded["bucket_name"], uploaded["object_name"])

    response = await test_client.post(
        "/api/knowledge/files/upload",
        files={"file": ("unsupported.ppt", b"unsupported", "application/vnd.ms-powerpoint")},
        headers=admin_headers,
    )
    assert response.status_code == 400, response.text
    assert "Unsupported file type: .ppt" in response.json()["detail"]

    response = await test_client.post("/api/chat/attachments/tmp", files=files, headers=admin_headers)
    assert response.status_code == 200, response.text
    attachment = response.json()
    bucket = minio.KB_BUCKETS["documents"]
    try:
        assert attachment["parse_supported"] is False
        assert attachment["parse_methods"] == []
        assert await minio.adownload_file(bucket, attachment["object_name"]) == original
        response = await test_client.post(
            "/api/chat/attachments/tmp/parse",
            json={"object_name": attachment["object_name"]},
            headers=admin_headers,
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "当前仅支持 PDF 和图片附件解析"
    finally:
        await minio.adelete_file(bucket, attachment["object_name"])
