"""知识库 Dashboard 聚合统计单元测试。"""

from types import SimpleNamespace

import pytest

from yuxi.services import knowledge_dashboard_service

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


async def test_knowledge_dashboard_uses_repository_aggregates(monkeypatch):
    """知识统计只编排 repository 聚合结果，不逐库加载文件。"""
    base_repository = SimpleNamespace(get_all=lambda: None, count_by_type=lambda: None)
    file_repository = SimpleNamespace(aggregate_dashboard_stats=lambda: None)

    async def get_all():
        return [SimpleNamespace(kb_id=kb_id) for kb_id in ("kb-a", "kb-b", "kb-c", "hidden")]

    async def count_by_type(*, kb_ids):
        assert kb_ids == ["kb-a", "kb-b", "kb-c"]
        return [("milvus", 2), ("custom", 1)]

    async def aggregate_dashboard_stats(*, kb_ids):
        assert kb_ids == ["kb-a", "kb-b", "kb-c"]
        return [("pdf", 3, 1200, 24), ("unknown", 1, 100, 0)]

    base_repository.get_all = get_all
    base_repository.count_by_type = count_by_type
    file_repository.aggregate_dashboard_stats = aggregate_dashboard_stats
    monkeypatch.setattr(knowledge_dashboard_service, "KnowledgeBaseRepository", lambda: base_repository)
    monkeypatch.setattr(knowledge_dashboard_service, "KnowledgeFileRepository", lambda: file_repository)
    monkeypatch.setattr(
        knowledge_dashboard_service,
        "resolve_knowledge_base_permission",
        lambda _subject, kb: (
            knowledge_dashboard_service.ResourcePermission.NONE
            if kb.kb_id == "hidden"
            else knowledge_dashboard_service.ResourcePermission.READ
        ),
    )

    stats = await knowledge_dashboard_service.get_knowledge_stats([{"uid": "user-1"}])

    assert stats == {
        "total_databases": 3,
        "total_files": 4,
        "total_nodes": 24,
        "total_storage_size": 1300,
        "databases_by_type": {"Milvus": 2, "custom": 1},
        "file_type_distribution": {"PDF文档": 3, "其他": 1},
    }
