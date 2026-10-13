"""知识库工具模块。"""

from typing import Any

from .kb_utils import (
    calculate_content_hash,
    is_minio_url,
    merge_processing_params,
    params_for_uploaded_document,
    parse_minio_url,
    prepare_item_metadata,
    resolve_processing_params,
    sanitize_processing_params,
)


def strip_nul_characters(value: Any) -> Any:
    """递归清理知识库 JSON 结果中的 NUL，并拒绝清理后重名的字段。"""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        cleaned = {}
        for key, child in value.items():
            cleaned_key = key.replace("\x00", "") if isinstance(key, str) else key
            if cleaned_key in cleaned:
                raise ValueError("知识库结果字段在 NUL 清理后发生重名")
            cleaned[cleaned_key] = strip_nul_characters(child)
        return cleaned
    if isinstance(value, list):
        return [strip_nul_characters(item) for item in value]
    if isinstance(value, tuple):
        return tuple(strip_nul_characters(item) for item in value)
    return value


__all__ = [
    "calculate_content_hash",
    "is_minio_url",
    "merge_processing_params",
    "params_for_uploaded_document",
    "parse_minio_url",
    "prepare_item_metadata",
    "resolve_processing_params",
    "sanitize_processing_params",
    "strip_nul_characters",
]
