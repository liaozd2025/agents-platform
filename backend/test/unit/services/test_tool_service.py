from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxi.agents.toolkits import service as tool_service


def test_get_tool_metadata_includes_config_guide(monkeypatch):
    tool_service._metadata_cache.clear()

    fake_tool = SimpleNamespace(
        name="demo_tool",
        description="demo description",
        metadata={},
        args_schema=None,
    )
    fake_extra = SimpleNamespace(
        category="buildin",
        tags=["demo"],
        display_name="演示工具",
        config_guide="请先配置 DEMO_API_KEY",
    )

    monkeypatch.setattr(
        "yuxi.agents.toolkits.registry.get_all_tool_instances",
        lambda: [fake_tool],
    )
    monkeypatch.setattr(
        "yuxi.agents.toolkits.registry.get_all_extra_metadata",
        lambda: {"demo_tool": fake_extra},
    )

    result = tool_service.get_tool_metadata()

    assert result == [
        {
            "slug": "demo_tool",
            "name": "演示工具",
            "description": "demo description",
            "metadata": {},
            "args": [],
            "category": "buildin",
            "tags": ["demo"],
            "config_guide": "请先配置 DEMO_API_KEY",
        }
    ]

    tool_service._metadata_cache.clear()


@pytest.mark.asyncio
async def test_runtime_tools_always_include_user_question_without_agent_configuration(monkeypatch):
    question_tool = SimpleNamespace(name="ask_user_question")
    monkeypatch.setattr(
        tool_service,
        "get_tool_instances_by_category",
        lambda category: [question_tool] if category == "buildin" else [],
    )

    tools = await tool_service.resolve_configured_runtime_tools(
        SimpleNamespace(tools=[], mcps=[], _runtime_skills={}, _effective_skill_slugs=[])
    )

    assert [tool.name for tool in tools] == ["ask_user_question"]
