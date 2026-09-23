from __future__ import annotations

import io
import zipfile

import pytest

from yuxi.knowledge.parser.archive_utils import (
    ArchiveFormatError,
    ArchiveScanReport,
    decode_entry_name,
    is_parsed_result_archive,
    iter_archive_entries,
)

pytestmark = pytest.mark.unit


def _make_zip(entries: dict[str, bytes], *, dirs: tuple[str, ...] = ()) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zip_file:
        for directory in dirs:
            zip_file.writestr(f"{directory}/", b"")
        for name, payload in entries.items():
            zip_file.writestr(name, payload)
    return buffer.getvalue()


def _strip_utf8_flag(data: bytes) -> bytes:
    """清掉 ZIP 头里的 UTF-8 标志位，模拟未声明编码的中文压缩包。

    ``zipfile`` 写非 ASCII 名字时总会置位 0x800，只能用字节级补丁复现
    「Windows/macOS 压缩工具把 UTF-8 字节按 CP437 存」的真实场景。
    """
    raw = bytearray(data)
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        index = raw.find(signature)
        while index != -1:
            flags = int.from_bytes(raw[index + flag_offset : index + flag_offset + 2], "little") & ~0x800
            raw[index + flag_offset : index + flag_offset + 2] = flags.to_bytes(2, "little")
            index = raw.find(signature, index + 4)
    return bytes(raw)


def test_decode_entry_name_recovers_chinese_from_cp437() -> None:
    # zipfile 把未声明编码的 UTF-8 字节按 CP437 解码后得到的乱码
    utf8_mojibake = "重庆大学医院酮洛芬资料".encode().decode("cp437")
    utf8_info = zipfile.ZipInfo(utf8_mojibake)
    utf8_info.flag_bits = 0
    assert decode_entry_name(utf8_info) == "重庆大学医院酮洛芬资料"

    # 中文 Windows 压缩工具常见：名字按 GBK 存储
    gbk_mojibake = "说明书".encode("gbk").decode("cp437")
    gbk_info = zipfile.ZipInfo(gbk_mojibake)
    gbk_info.flag_bits = 0
    assert decode_entry_name(gbk_info) == "说明书"

    # 已声明 UTF-8 标志位时原样返回
    ascii_info = zipfile.ZipInfo("report.pdf")
    ascii_info.flag_bits = 0x800
    assert decode_entry_name(ascii_info) == "report.pdf"


def test_archive_metadata_and_unsupported_entries_are_skipped() -> None:
    payload = _make_zip(
        {
            "资料/说明书.docx": b"docx-bytes",
            "资料/彩页.pdf": b"pdf-bytes",
            "资料/.DS_Store": b"junk",
            "资料/._说明书.docx": b"appledouble",
            "__MACOSX/资料/._说明书.docx": b"appledouble",
            "资料/Thumbs.db": b"junk",
            "资料/installer.exe": b"binary",
            "资料/readme": b"no-extension",
            "资料/nested.zip": b"pk",
        },
        dirs=("资料",),
    )

    report = ArchiveScanReport()
    entries = list(iter_archive_entries(payload, report=report))

    assert [entry.filename for entry in entries] == ["说明书.docx", "彩页.pdf"]
    assert report.skipped
    assert {item["reason"] for item in report.skipped} == {"archive_metadata", "unsupported_type"}


def test_duplicate_basenames_from_different_folders_are_disambiguated() -> None:
    payload = _make_zip(
        {
            "接待日资料/酮洛芬彩页.pdf": b"first",
            "预约资料/酮洛芬彩页.pdf": b"second",
            "预约资料/酮洛芬彩页(2).pdf": b"third",
        }
    )

    entries = list(iter_archive_entries(payload))

    assert [entry.filename for entry in entries] == [
        "酮洛芬彩页.pdf",
        "酮洛芬彩页(2).pdf",
        "酮洛芬彩页(2)(2).pdf",
    ]
    # 内容与包内路径仍各自独立
    assert entries[0].inner_path == "接待日资料/酮洛芬彩页.pdf"
    assert entries[1].data == b"second"


def test_cp437_archive_filenames_survive_end_to_end() -> None:
    payload = _strip_utf8_flag(
        _make_zip({"重庆大学医院酮洛芬资料/预约资料/平台备案表.pdf": b"pdf-bytes"})
    )

    entries = list(iter_archive_entries(payload))

    assert [entry.filename for entry in entries] == ["平台备案表.pdf"]
    assert entries[0].inner_path == "重庆大学医院酮洛芬资料/预约资料/平台备案表.pdf"


def test_is_parsed_result_archive_distinguishes_mineru_bundles() -> None:
    mineru_bundle = _make_zip({"full.md": b"# doc", "images/a.png": b"png"})
    plain_archive = _make_zip({"说明书.docx": b"docx"})

    assert is_parsed_result_archive(mineru_bundle) is True
    assert is_parsed_result_archive(plain_archive) is False
    assert is_parsed_result_archive(b"not-a-zip") is False


def test_unsafe_paths_are_rejected() -> None:
    payload = _make_zip({"../escape.pdf": b"pdf"})

    with pytest.raises(ArchiveFormatError):
        list(iter_archive_entries(payload))


def test_broken_archive_is_reported_as_format_error() -> None:
    with pytest.raises(ArchiveFormatError):
        list(iter_archive_entries(b"definitely-not-a-zip"))


def test_entry_limit_is_enforced() -> None:
    payload = _make_zip({f"doc-{index}.pdf": b"pdf" for index in range(6)})

    with pytest.raises(ArchiveFormatError):
        list(iter_archive_entries(payload, max_entries=5))


def test_oversized_entry_is_skipped_instead_of_failing() -> None:
    payload = _make_zip({"big.pdf": b"x" * 64, "small.pdf": b"y"})

    report = ArchiveScanReport()
    entries = list(iter_archive_entries(payload, report=report, max_entry_bytes=16))

    assert [entry.filename for entry in entries] == ["small.pdf"]
    assert report.skipped == [{"filename": "big.pdf", "reason": "entry_too_large"}]


def test_archive_without_importable_entries_returns_empty_list() -> None:
    payload = _make_zip({"__MACOSX/._x.docx": b"junk", ".DS_Store": b"junk"})

    assert list(iter_archive_entries(payload)) == []
