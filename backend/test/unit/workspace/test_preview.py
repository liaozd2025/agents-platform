"""Workspace 文件预览在疑似加密 / 损坏 Office 文件上的快速失败约束。"""

from __future__ import annotations

from yuxi.services.file_preview import render_file_preview
from yuxi.workspace import preview as preview_module
from yuxi.workspace.preview import preview_workspace_file

# 本机实测的企业加密（DLP）密文头：既不是 zip 也不是 OLE2，但后缀仍是 Office 扩展名。
_ENCRYPTED_BYTES = b"\x63\xc0\xb6\x4d\x0d\x50\xc1\x2f" + b"\x00" * 64


def _forbid_libreoffice(monkeypatch) -> list[str]:
    """把 LibreOffice 转换替换为记录器：一旦被调用就说明快速失败没生效。"""
    calls: list[str] = []

    def _record(filename: str, content: bytes) -> bytes:
        calls.append(filename)
        raise AssertionError("疑似加密文件不应触发 LibreOffice 转换")

    monkeypatch.setattr(preview_module, "convert_office_to_pdf", _record)
    return calls


async def test_encrypted_pptx_returns_encryption_hint_without_converting(monkeypatch):
    calls = _forbid_libreoffice(monkeypatch)

    result = await preview_workspace_file(
        "/home/gem/user-data/shared/u1/workspace/projects/p1/uploads/黑豆+黑养膏.pptx",
        _ENCRYPTED_BYTES,
        office_cache_key="unit:encrypted-pptx",
    )

    assert calls == []
    assert result.supported is False
    assert result.preview_type == "unsupported"
    assert result.content is None
    assert "加密" in (result.message or "")


async def test_encrypted_xlsx_skips_spreadsheet_parser(monkeypatch):
    """表格后缀同样先过签名校验，不进入 openpyxl 解析进程。"""

    def _fail(*args, **kwargs):
        raise AssertionError("疑似加密表格不应进入表格解析")

    monkeypatch.setattr(preview_module, "render_preview", _fail)

    result = await preview_workspace_file(
        "uploads/台账.xlsx",
        _ENCRYPTED_BYTES,
        office_cache_key="unit:encrypted-xlsx",
    )

    assert result.supported is False
    assert "加密" in (result.message or "")


async def test_valid_ooxml_pptx_still_converts_to_pdf(monkeypatch, tmp_path):
    """合法 OOXML 的转换路径不被本次守卫影响。"""
    monkeypatch.setattr(preview_module, "get_runtime_dir", lambda: tmp_path)
    converted: list[str] = []

    async def _fake_convert(filename: str, content: bytes) -> bytes:
        converted.append(filename)
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(preview_module, "convert_office_to_pdf", _fake_convert)

    result = await preview_workspace_file(
        "uploads/产品介绍.pptx",
        b"PK\x03\x04demo",
        office_cache_key="unit:valid-pptx",
    )

    assert converted == ["产品介绍.pptx"]
    assert result.supported is True
    assert result.preview_type == "pdf"
    assert result.content == b"%PDF-1.4 fake"


async def test_render_file_preview_returns_payload_with_encryption_hint():
    """HTTP 层返回结构化 payload，前端据此展示 message（而不是通用错误文案）。"""
    payload = await render_file_preview(
        "/home/gem/user-data/shared/u1/workspace/projects/p1/uploads/黑豆+黑养膏.pptx",
        _ENCRYPTED_BYTES,
        office_cache_key="unit:render-payload",
    )

    assert isinstance(payload, dict)
    assert payload["supported"] is False
    assert payload["preview_type"] == "unsupported"
    assert "加密" in payload["message"]
