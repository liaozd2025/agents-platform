from __future__ import annotations

from io import BytesIO

import pytest
from docx import Document

from yuxi.utils.filepreview import (
    MAX_TEXT_PREVIEW_CHARS,
    detect_preview_type,
    is_office_pdf_preview_file,
    office_container_signature_mismatch,
    render_preview,
)


def _build_docx_bytes(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("renderer", "expected_third"),
    [
        (detect_preview_type, "当前文件是二进制文件，暂不支持预览"),
        (render_preview, None),
    ],
)
def test_docx_is_not_treated_as_markdown_preview(renderer, expected_third):
    result = renderer("demo.docx", _build_docx_bytes("Docx preview"))

    if isinstance(result, tuple):
        preview_type, supported, third = result
    else:
        preview_type, supported, third = result.preview_type, result.supported, result.content

    assert preview_type == "unsupported"
    assert supported is False
    assert third == expected_third


def test_render_preview_truncates_long_markdown():
    result = render_preview("note.md", ("x" * (MAX_TEXT_PREVIEW_CHARS + 1)).encode("utf-8"))

    assert result.preview_type == "markdown"
    assert result.supported is True
    assert result.truncated is True
    assert result.limit == MAX_TEXT_PREVIEW_CHARS
    assert len(result.content) == MAX_TEXT_PREVIEW_CHARS


def test_render_preview_returns_complete_binary_result_from_signature():
    content = b"%PDF-1.4\npreview"

    result = render_preview("report.bin", content)

    assert result.content == content
    assert result.preview_type == "pdf"
    assert result.supported is True
    assert result.media_type == "application/pdf"
    assert result.filename == "report.bin"


def test_render_preview_keeps_unsupported_binary_content_hidden():
    result = render_preview("archive.bin", b"\x00binary")

    assert result.content is None
    assert result.preview_type == "unsupported"
    assert result.supported is False


def test_office_pdf_preview_includes_legacy_word_and_powerpoint():
    assert is_office_pdf_preview_file("demo.docx") is True
    assert is_office_pdf_preview_file("demo.pptx") is True
    assert is_office_pdf_preview_file("demo.xlsx") is False
    assert is_office_pdf_preview_file("demo.doc") is True
    assert is_office_pdf_preview_file("demo.ppt") is True


# 本机实测的企业加密（DLP）密文头：既不是 zip 也不是 OLE2，后缀仍保留 Office 扩展名。
_ENCRYPTED_HEAD = b"\x63\xc0\xb6\x4d\x0d\x50\xc1\x2f"


@pytest.mark.parametrize("path", ["黑豆+黑养膏.pptx", "demo.docx", "demo.xlsx", "demo.doc", "demo.ppt", "demo.xls"])
def test_office_signature_mismatch_flags_encrypted_containers(path):
    assert office_container_signature_mismatch(path, _ENCRYPTED_HEAD + b"\x00" * 16) is True


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("demo.pptx", b"PK\x03\x04rest"),
        ("demo.xlsx", b"PK\x03\x04rest"),
        # 旧格式内容配 OOXML 后缀（改名文件）与带密码的 OOXML 都是 OLE2，放行交给 LibreOffice 判断
        ("demo.docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest"),
        ("demo.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest"),
    ],
)
def test_office_signature_mismatch_accepts_known_office_containers(path, content):
    assert office_container_signature_mismatch(path, content) is False


@pytest.mark.parametrize(
    ("path", "content"),
    [("note.md", _ENCRYPTED_HEAD), ("report.pdf", b"%PDF-1.4"), ("a.txt", b"\x00\x01")],
)
def test_office_signature_mismatch_ignores_non_office_suffix(path, content):
    assert office_container_signature_mismatch(path, content) is False
