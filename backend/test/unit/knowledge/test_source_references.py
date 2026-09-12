from yuxi.knowledge.source_references import build_knowledge_source_reference


def test_build_knowledge_source_reference_uses_oa_article_header() -> None:
    source_ref = build_knowledge_source_reference(
        "task_id        2191146\n"
        "title          新闻详细-2191146\n"
        "author_name    刘从新\n"
        "type_name      新闻动态\n\n"
        "正文内容\n"
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
        'task_id  "2441705.0"\n'
        'title  "秋季高发！该怎么拯救我的过敏性鼻炎？"\n'
        "type_name  新闻动态\n"
    )

    assert source_ref["title"] == "秋季高发！该怎么拯救我的过敏性鼻炎？"
    assert source_ref["url"] == (
        "https://hnjiudian.cn/web/index.html#/corporate-culture/view-page/3?"
        "title=%E6%96%B0%E9%97%BB%E8%AF%A6%E7%BB%86-2441705&taskID=2441705&ecType=3"
    )


def test_build_knowledge_source_reference_uses_goodarticles_page_type() -> None:
    source_ref = build_knowledge_source_reference(
        "task_id  3104861\n"
        "title  讲九典故事 扬文化风帆\n"
        "type_name  goodarticles\n"
    )

    assert "/view-page/1?" in source_ref["url"]
    assert "taskID=3104861" in source_ref["url"]
    assert "ecType=1" in source_ref["url"]


def test_build_knowledge_source_reference_uses_good_articles_page_type() -> None:
    source_ref = build_knowledge_source_reference(
        "task_id  3104861\n"
        "title  讲九典故事 扬文化风帆\n"
        "type_name  good_articles\n"
    )

    assert "/view-page/1?" in source_ref["url"]
    assert "taskID=3104861" in source_ref["url"]
    assert "ecType=1" in source_ref["url"]
