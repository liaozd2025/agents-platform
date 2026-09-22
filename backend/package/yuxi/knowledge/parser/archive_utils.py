"""归档包（ZIP）批量导入支持。

知识库对 ZIP 有两种语义，按包内内容自动区分：

1. **解析结果包**：MinerU 等解析工具产出的包，包内存在 ``full.md``。
   走 :mod:`yuxi.knowledge.parser.zip_utils`，提取 Markdown 与图片，合并为**单个**文档。
2. **资料归档包**：普通压缩包，包内是 PDF/DOCX 等原始文档。
   由本模块展开，包内每个受支持的文件作为**独立**文档导入。

本模块只负责第 2 种语义的「识别 + 展开 + 命名」，不负责上传与入库。
"""

from __future__ import annotations

import io
import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from yuxi.knowledge.parser.capabilities import SUPPORTED_FILE_EXTENSIONS

#: 包内出现该文件即视为「解析结果包」，交给 zip_utils 处理。
PARSED_RESULT_MARKER = "full.md"

#: 单个包最多展开多少条可导入条目。
MAX_ARCHIVE_ENTRIES = 500

#: 单个条目的解压后大小上限。
MAX_ARCHIVE_ENTRY_BYTES = 200 * 1024 * 1024

#: 整个包解压后的累计大小上限（防 zip bomb）。
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024

#: 大小写不敏感的忽略文件名（压缩工具/操作系统的元数据）。
_IGNORED_BASENAMES = frozenset({"thumbs.db", "desktop.ini", ".ds_store"})

#: 忽略的目录前缀（macOS 打包时写入的资源分支目录）。
_IGNORED_PATH_PREFIXES = ("__macosx/",)


class ArchiveFormatError(ValueError):
    """归档包本身无法读取或结构非法。"""


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """一个可导入的归档条目。"""

    inner_path: str
    """包内相对路径（已修正文件名编码，可能含有子目录）。"""

    filename: str
    """用于入库的文件名（已扁平化并去重）。"""

    data: bytes
    """文件内容。"""


@dataclass(slots=True)
class ArchiveScanReport:
    """展开过程中的跳过记录，用于向用户解释「为什么少了几个文件」。"""

    skipped: list[dict[str, str]] = field(default_factory=list)
    total_bytes: int = 0

    def skip(self, name: str, reason: str) -> None:
        self.skipped.append({"filename": name, "reason": reason})


def decode_entry_name(info: zipfile.ZipInfo) -> str:
    """还原 ZIP 条目的文件名。

    ZIP 规范里非 ASCII 文件名要么带 UTF-8 标志位，要么按 CP437 编码存储。
    Windows / macOS 的中文压缩包普遍**没有**设置 UTF-8 标志位，``zipfile``
    会把原始字节按 CP437 解码，于是「重庆」变成「Θçìσ║å」。这里把 CP437
    字符串还原成原始字节，再按 UTF-8 / GBK / Big5 逐个尝试解码。
    """
    name = info.filename
    if not name or info.flag_bits & 0x800:
        return name

    try:
        raw_bytes = name.encode("cp437")
    except UnicodeEncodeError:
        return name

    for encoding in ("utf-8", "gbk", "big5"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return name


def _normalize_inner_path(name: str) -> str:
    return name.replace("\\", "/").lstrip("/")


def _is_ignored(inner_path: str) -> bool:
    """判断条目是否为压缩工具或操作系统写入的元数据。"""
    lowered = inner_path.lower()
    if lowered.startswith(_IGNORED_PATH_PREFIXES):
        return True

    parts = [part for part in lowered.split("/") if part]
    if not parts:
        return True
    # 隐藏文件/隐藏目录（.DS_Store、.git 等）
    if any(part.startswith(".") for part in parts):
        return True
    # macOS 的 AppleDouble 伴生文件，形如 ._report.pdf
    if parts[-1].startswith("._"):
        return True
    return parts[-1] in _IGNORED_BASENAMES


def _is_importable(inner_path: str) -> bool:
    suffix = Path(inner_path).suffix.lower()
    if not suffix:
        return False
    # 嵌套压缩包不递归展开，避免 zip bomb 与语义歧义
    if suffix == ".zip":
        return False
    return suffix in SUPPORTED_FILE_EXTENSIONS


def is_parsed_result_archive(data: bytes) -> bool:
    """判断归档包是否为解析工具产出的结果包（包内含 ``full.md``）。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zip_file:
            for info in zip_file.infolist():
                if info.is_dir():
                    continue
                if Path(decode_entry_name(info)).name.lower() == PARSED_RESULT_MARKER:
                    return True
    except zipfile.BadZipFile:
        return False
    return False


def iter_archive_entries(
    data: bytes,
    *,
    report: ArchiveScanReport | None = None,
    max_entries: int = MAX_ARCHIVE_ENTRIES,
    max_entry_bytes: int = MAX_ARCHIVE_ENTRY_BYTES,
    max_total_bytes: int = MAX_ARCHIVE_UNCOMPRESSED_BYTES,
) -> Iterator[ArchiveEntry]:
    """逐个产出归档包内可导入的文件。

    逐条读取而不是一次性解压，内存占用由「最大的单个文件」决定。

    Raises:
        ArchiveFormatError: 包无法读取、含不安全路径、或规模超出限制。
    """
    try:
        zip_file = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchiveFormatError("压缩包无法解析，请确认文件完整且为 ZIP 格式") from exc

    with zip_file:
        infos = zip_file.infolist()

        # 路径安全校验：与 zip_utils 保持同一标准
        for info in infos:
            name = _normalize_inner_path(decode_entry_name(info))
            if name.startswith("/") or ".." in Path(name).parts:
                raise ArchiveFormatError(f"压缩包包含不安全路径: {name}")

        used_names: set[str] = set()
        entry_count = 0
        total_bytes = 0

        for info in infos:
            if info.is_dir():
                continue

            inner_path = _normalize_inner_path(decode_entry_name(info))
            if not inner_path:
                continue

            if _is_ignored(inner_path):
                if report is not None:
                    report.skip(inner_path, "archive_metadata")
                continue

            if not _is_importable(inner_path):
                if report is not None:
                    report.skip(inner_path, "unsupported_type")
                continue

            if info.file_size > max_entry_bytes:
                if report is not None:
                    report.skip(inner_path, "entry_too_large")
                continue

            entry_count += 1
            if entry_count > max_entries:
                raise ArchiveFormatError(
                    f"压缩包内文件过多（超过 {max_entries} 个），请拆分后再上传"
                )

            total_bytes += info.file_size
            if total_bytes > max_total_bytes:
                raise ArchiveFormatError("压缩包解压后体积过大，请拆分后再上传")

            try:
                with zip_file.open(info) as handle:
                    entry_data = handle.read()
            except Exception:  # noqa: BLE001 - 单条损坏不应影响整个包
                if report is not None:
                    report.skip(inner_path, "read_failed")
                continue

            if report is not None:
                report.total_bytes = total_bytes

            yield ArchiveEntry(
                inner_path=inner_path,
                filename=_dedupe_filename(Path(inner_path).name, used_names),
                data=entry_data,
            )


def _dedupe_filename(basename: str, used_names: set[str]) -> str:
    """同名文件（不同子目录）追加序号，避免入库后无法区分。

    生成的新名字也会写回 ``used_names``，因此包内本身就存在
    ``彩页.pdf`` 与 ``彩页(2).pdf`` 时不会再次撞名。
    """
    key = basename.lower()
    if key not in used_names:
        used_names.add(key)
        return basename

    stem, suffix = os.path.splitext(basename)
    index = 2
    while True:
        candidate = f"{stem}({index}){suffix}"
        candidate_key = candidate.lower()
        if candidate_key not in used_names:
            used_names.add(candidate_key)
            return candidate
        index += 1
