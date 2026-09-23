"""通过真实 HTTP 与 MinIO 验证 ZIP 资料包的解压上传。"""

import io
import uuid
import zipfile

import pytest
from yuxi.storage.minio.client import get_minio_client

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _build_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zip_file:
        zip_file.writestr("资料/说明书.docx", b"docx-content")
        zip_file.writestr("资料/彩页.pdf", b"pdf-content")
        zip_file.writestr("接待资料/彩页.pdf", b"second-pdf-content")
        # 压缩工具与操作系统写入的元数据
        zip_file.writestr("资料/.DS_Store", b"junk")
        zip_file.writestr("__MACOSX/资料/._彩页.pdf", b"junk")
        # 不受支持的格式
        zip_file.writestr("资料/installer.exe", b"binary")
    return buffer.getvalue()


async def test_archive_upload_expands_into_multiple_documents(test_client, admin_headers):
    """普通资料包解压后，包内每个文件成为独立文档，元数据被跳过。"""
    archive = _build_archive()
    filename = f"pytest_archive_{uuid.uuid4().hex}.zip"
    minio = get_minio_client()

    response = await test_client.post(
        "/api/knowledge/files/upload",
        files={"file": (filename, archive, "application/zip")},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["is_archive"] is True
    assert payload["archive_filename"] == filename.lower()
    assert payload["uploaded_count"] == 3

    items = payload["items"]
    assert [item["filename"] for item in items] == ["说明书.docx", "彩页.pdf", "彩页(2).pdf"]
    assert {item["inner_path"] for item in items} == {
        "资料/说明书.docx",
        "资料/彩页.pdf",
        "接待资料/彩页.pdf",
    }

    try:
        expected = [b"docx-content", b"pdf-content", b"second-pdf-content"]
        for item, content in zip(items, expected, strict=True):
            assert item["content_hash"]
            assert item["size"] == len(content)
            stored = await minio.adownload_file(item["bucket_name"], item["object_name"])
            assert stored == content
    finally:
        for item in items:
            await minio.adelete_file(item["bucket_name"], item["object_name"])

    skipped = {(entry["filename"], entry["reason"]) for entry in payload["skipped_items"]}
    assert ("资料/.DS_Store", "archive_metadata") in skipped
    assert ("__MACOSX/资料/._彩页.pdf", "archive_metadata") in skipped
    assert ("资料/installer.exe", "unsupported_type") in skipped


async def test_mineru_result_archive_keeps_single_document_semantics(test_client, admin_headers):
    """含 full.md 的解析结果包仍按单文档上传，不做多文档展开。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zip_file:
        zip_file.writestr("full.md", "# Parsed document\n")
        zip_file.writestr("images/figure.png", b"png-bytes")
    filename = f"pytest_mineru_{uuid.uuid4().hex}.zip"

    response = await test_client.post(
        "/api/knowledge/files/upload",
        files={"file": (filename, buffer.getvalue(), "application/zip")},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert "is_archive" not in payload
    assert payload["filename"] == filename.lower()

    minio = get_minio_client()
    try:
        await minio.adelete_file(payload["bucket_name"], payload["object_name"])
    except Exception:  # noqa: BLE001 - 清理失败不影响断言结论
        pass
