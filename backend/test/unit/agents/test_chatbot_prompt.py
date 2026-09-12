from types import SimpleNamespace

from yuxi.agents.buildin.chatbot.prompt import PROMPT, build_prompt_with_context


def test_chatbot_prompt_does_not_duplicate_html_preview_skill_instructions():
    assert "html:preview" not in PROMPT


def test_default_chatbot_identifies_as_jiudian_ai_assistant():
    system_prompt = build_prompt_with_context(
        SimpleNamespace(system_prompt="", workdir_path="/home/gem/user-data/projects/demo")
    )

    assert "你是九典AI助手" in system_prompt
    assert "当用户问你是谁时，只回答“我是九典AI助手”，不要补充其他内容" in system_prompt
    assert "语析" not in system_prompt


def test_chatbot_prompt_declares_workspace_visibility_and_default_write_boundary():
    prompt = build_prompt_with_context(
        SimpleNamespace(
            workdir_path="/home/gem/user-data/projects/11111111-1111-4111-8111-111111111111",
            system_prompt="",
        )
    )

    assert "可以读取其他 Project 目录作为参考" in prompt
    assert "未经用户明确要求，不得在当前 Project Workdir 之外" in prompt
    assert "/home/gem/user-data/agents/skills/" in prompt


def test_chatbot_prompt_requires_structured_interrupt_for_blocking_user_input():
    prompt = build_prompt_with_context(
        SimpleNamespace(system_prompt="", workdir_path="/home/gem/user-data/projects/demo")
    )

    assert "必须调用 `ask_user_question` 进入等待状态" in prompt
    assert "不要只发送问题文本后结束本轮" in prompt
