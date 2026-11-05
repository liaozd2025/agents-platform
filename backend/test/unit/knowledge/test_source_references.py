from urllib.parse import parse_qs

from yuxi.modules.knowledge.source_references import build_knowledge_source_reference


def test_source_reference_removes_nul_before_encoding_oa_url() -> None:
    """OA 参数编码前清理 NUL，防止残留不可识别的任务号和标题。"""
    source_ref = build_knowledge_source_reference("task_id  12\x003\ntitle  原\x00文\ntype_name  好文\x00共享\n")

    assert source_ref["title"] == "原文"
    assert source_ref["type_name"] == "好文共享"
    assert parse_qs(source_ref["url"].split("?", 1)[1]) == {
        "title": ["原文"],
        "taskID": ["123"],
        "ecType": ["1"],
    }


def test_build_knowledge_source_reference_uses_oa_article_header() -> None:
    source_ref = build_knowledge_source_reference(
        "task_id        2191146\ntitle          新闻详细-2191146\nauthor_name    刘从新\ntype_name      新闻动态\n\n正文内容\n"
    )

    assert source_ref == {
        "source_type": "knowledge_base",
        "title": "新闻详细-2191146",
        "author_name": "刘从新",
        "type_name": "新闻动态",
        "url": (
            "https://hnjiudian.cn/web/index.html#/corporate-culture/view-page/3?"
            "title=%E6%96%B0%E9%97%BB%E8%AF%A6%E7%BB%86-2191146&taskID=2191146&ecType=3"
        ),
    }


def test_build_knowledge_source_reference_without_task_id_has_no_url() -> None:
    source_ref = build_knowledge_source_reference("title  仅有标题", fallback_title="fallback.md")

    assert source_ref == {"source_type": "knowledge_base", "title": "仅有标题"}


def test_build_knowledge_source_reference_normalizes_news_oa_route() -> None:
    source_ref = build_knowledge_source_reference(
        'task_id  "2441705.0"\ntitle  "秋季高发！该怎么拯救我的过敏性鼻炎？"\ntype_name  新闻动态\n'
    )

    assert source_ref["title"] == "秋季高发！该怎么拯救我的过敏性鼻炎？"
    assert source_ref["url"] == (
        "https://hnjiudian.cn/web/index.html#/corporate-culture/view-page/3?"
        "title=%E6%96%B0%E9%97%BB%E8%AF%A6%E7%BB%86-2441705&taskID=2441705&ecType=3"
    )


def test_build_knowledge_source_reference_uses_goodarticles_page_type() -> None:
    source_ref = build_knowledge_source_reference("task_id  3104861\ntitle  讲九典故事 扬文化风帆\ntype_name  goodarticles\n")

    assert "/view-page/1?" in source_ref["url"]
    assert "taskID=3104861" in source_ref["url"]
    assert "ecType=1" in source_ref["url"]


def test_build_knowledge_source_reference_uses_good_articles_page_type() -> None:
    source_ref = build_knowledge_source_reference("task_id  3104861\ntitle  讲九典故事 扬文化风帆\ntype_name  good_articles\n")

    assert "/view-page/1?" in source_ref["url"]
    assert "taskID=3104861" in source_ref["url"]
    assert "ecType=1" in source_ref["url"]


async def test_query_source_metadata_reaches_turn_reference_projection(monkeypatch):
    """已授权 query 的 OA 文章标题和地址进入新版 Turn 引用来源。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from yuxi.modules.knowledge.mcp.references import knowledge_reference_sources
    from yuxi.modules.knowledge.services import tools

    manager = SimpleNamespace(
        retrieve=AsyncMock(
            return_value={
                "kb_id": "kb",
                "results": [{"id": "chunk", "file_id": "file", "content": "正文", "metadata": {"source": "original.md"}}],
            }
        ),
        get_file_info=AsyncMock(return_value={"content": "title: 文章标题\ntask_id: 42\ntype_name: 好文共享"}),
    )
    monkeypatch.setattr(tools, "knowledge_base", manager)
    result = await tools.query_kb("kb", "正文", [{"kb_id": "kb", "name": "企业文章"}])
    sources = knowledge_reference_sources(result, "query_kb", "kb")

    assert sources[0]["title"] == "文章标题"
    assert "taskID=42" in sources[0]["url"]
    assert sources[0]["content"] == "正文"
    assert sources[0]["file_id"] == "file"


def test_turn_reference_rejects_unsafe_source_url():
    """模型可见来源中的非网页 URL 不能变成可点击跳转。"""
    from yuxi.modules.knowledge.mcp.references import knowledge_reference_sources

    payload = {
        "results": [{"file_id": "file", "content": "正文", "metadata": {"source_ref": {"title": "标题", "url": "javascript:alert(1)"}}}]
    }
    sources = knowledge_reference_sources(payload, "query_kb", "kb")
    assert sources[0]["title"] == "标题"
    assert "url" not in sources[0]


def test_find_turn_reference_preserves_document_title():
    """Find 原文窗口沿用文件级标题，不退回文件 ID。"""
    from yuxi.modules.knowledge.mcp.references import knowledge_reference_sources

    payload = {"title": "文档标题", "file_id": "file", "windows": [{"start_line": 1, "end_line": 1, "content": "1\t正文"}]}
    source = knowledge_reference_sources(payload, "find_kb_document", "kb")[0]
    assert source["title"] == "文档标题"
    assert source["content"] == "正文"
