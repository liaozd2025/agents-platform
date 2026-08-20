"""知识域 Dashboard 统计用例。"""

from typing import Any

from yuxi.permissions import ResourcePermission, resolve_knowledge_base_permission
from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository
from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository


async def get_knowledge_stats(subjects: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总授权主体可见的知识库、文件类型与存储大小统计。"""

    knowledge_bases = [
        knowledge_base
        for knowledge_base in await KnowledgeBaseRepository().get_all()
        if any(
            resolve_knowledge_base_permission(subject, knowledge_base) != ResourcePermission.NONE
            for subject in subjects
        )
    ]
    file_repository = KnowledgeFileRepository()
    databases_by_type: dict[str, int] = {}
    files_by_type: dict[str, int] = {}
    total_files = 0
    total_storage_size = 0

    file_type_mapping = {
        "txt": "文本文件",
        "pdf": "PDF文档",
        "docx": "Word文档",
        "doc": "Word文档",
        "md": "Markdown",
        "html": "HTML网页",
        "htm": "HTML网页",
        "json": "JSON数据",
        "csv": "CSV表格",
        "xlsx": "Excel表格",
        "xls": "Excel表格",
        "pptx": "PowerPoint",
        "ppt": "PowerPoint",
        "png": "PNG图片",
        "jpg": "JPEG图片",
        "jpeg": "JPEG图片",
        "gif": "GIF图片",
        "svg": "SVG图片",
        "mp4": "MP4视频",
        "mp3": "MP3音频",
        "zip": "ZIP压缩包",
        "rar": "RAR压缩包",
        "7z": "7Z压缩包",
    }
    database_type_mapping = {
        "faiss": "FAISS",
        "milvus": "Milvus",
        "dify": "Dify",
        "qdrant": "Qdrant",
        "elasticsearch": "Elasticsearch",
        "unknown": "未知类型",
    }

    for knowledge_base in knowledge_bases:
        database_type = (knowledge_base.kb_type or "unknown").lower()
        display_type = database_type_mapping.get(database_type, knowledge_base.kb_type or "未知类型")
        databases_by_type[display_type] = databases_by_type.get(display_type, 0) + 1

        files = await file_repository.list_by_kb_id(knowledge_base.kb_id)
        total_files += len(files)
        for record in files:
            file_extension = (record.file_type or "").lower()
            display_name = file_type_mapping.get(
                file_extension,
                file_extension.upper() + "文件" if file_extension else "其他",
            )
            files_by_type[display_name] = files_by_type.get(display_name, 0) + 1
            total_storage_size += int(record.file_size or 0)

    return {
        "total_databases": len(knowledge_bases),
        "total_files": total_files,
        "total_nodes": 0,
        "total_storage_size": total_storage_size,
        "databases_by_type": databases_by_type,
        "file_type_distribution": files_by_type,
    }
