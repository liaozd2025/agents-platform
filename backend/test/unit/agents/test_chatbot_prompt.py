from types import SimpleNamespace

from yuxi.agents.buildin.chatbot.prompt import PROMPT, build_prompt_with_context


def test_chatbot_prompt_does_not_duplicate_html_preview_skill_instructions():
    assert "html:preview" not in PROMPT


def test_default_chatbot_identifies_as_jiudian_ai_assistant():
    system_prompt = build_prompt_with_context(SimpleNamespace(system_prompt=""))

    assert "你是九典AI助手" in system_prompt
    assert "当用户问你是谁时，只回答“我是九典AI助手”，不要补充其他内容" in system_prompt
    assert "语析" not in system_prompt
