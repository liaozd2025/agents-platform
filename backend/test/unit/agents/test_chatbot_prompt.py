from types import SimpleNamespace

from yuxi.agents.context import DEFAULT_SYSTEM_PROMPT_PLACEHOLDER, is_custom_system_prompt
from yuxi.agents.buildin.chatbot.prompt import (
    CUSTOM_PROMPT_HEADER,
    DEFAULT_IDENTITY_PROMPT,
    build_prompt_with_context,
)


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
    assert "html:preview" not in prompt


def test_chatbot_prompt_requires_structured_interrupt_for_blocking_user_input():
    prompt = build_prompt_with_context(
        SimpleNamespace(system_prompt="", workdir_path="/home/gem/user-data/projects/demo")
    )

    assert "必须调用 `ask_user_question` 进入等待状态" in prompt
    assert "不要只发送问题文本后结束本轮" in prompt


def test_custom_system_prompt_takes_over_agent_identity():
    """Agent 配置自定义系统提示词后，默认身份必须让位，否则身份问题仍回默认品牌。

    负向守卫：若把默认身份改回无条件注入，本用例会因出现默认身份锁定句而失败。
    """
    prompt = build_prompt_with_context(
        SimpleNamespace(
            system_prompt="你是采购合规助手，只回答采购合规相关问题。",
            workdir_path="/home/gem/user-data/projects/demo",
        )
    )

    # 默认身份的锁定句必须完全消失，不能只靠模型自行权衡
    assert DEFAULT_IDENTITY_PROMPT.strip() not in prompt
    assert "只回答“我是九典AI助手”" not in prompt
    # 自定义设定需在最高优先级区块内，且位于整个 system prompt 末尾
    assert CUSTOM_PROMPT_HEADER in prompt
    assert prompt.rstrip().endswith("你是采购合规助手，只回答采购合规相关问题。")
    # 平台契约不因自定义而丢失
    assert "必须调用 `ask_user_question` 进入等待状态" in prompt
    assert "未经用户明确要求，不得在当前 Project Workdir 之外" in prompt


def test_workspace_appended_prompt_does_not_suppress_default_identity():
    """工作区 AGENTS.md/USER.md 被追加进 system_prompt 后，不得被误判为「已自定义」。

    build_agent_input_context 会把工作区文件内容追加到 system_prompt，未配置提示词的
    智能体也会因此拿到一段非空文本。此时必须依据运行时标志判定，否则默认身份被顶掉，
    模型只能泛泛自称「我是一个 AI 助手」。

    负向守卫：若改回按 system_prompt 内容判定，本用例会失败。
    """
    workspace_block = (
        "用户工作区 agents/AGENTS.md 内容：\n# AGENTS\n\n以下是约束 Agent 行为的一些要求\n\n"
        "用户工作区 agents/USER.md 内容：\n# 关于我\n"
    )
    prompt = build_prompt_with_context(
        SimpleNamespace(
            workdir_path="/home/gem/user-data/projects/demo",
            system_prompt=workspace_block,
            system_prompt_is_custom=False,
        )
    )

    assert "当用户问你是谁时，只回答“我是九典AI助手”，不要补充其他内容" in prompt
    assert CUSTOM_PROMPT_HEADER not in prompt
    # 工作区内容仍需保留在末尾作为补充设定
    assert "以下是约束 Agent 行为的一些要求" in prompt


def test_explicit_custom_flag_wins_over_appended_workspace_text():
    """显式配置了系统提示词时，追加工作区内容不影响「自定义」结论。"""
    workspace_block = "用户工作区 agents/AGENTS.md 内容：\n# AGENTS\n"
    prompt = build_prompt_with_context(
        SimpleNamespace(
            workdir_path="/home/gem/user-data/projects/demo",
            system_prompt="你是采购合规助手。" + workspace_block,
            system_prompt_is_custom=True,
        )
    )

    assert "只回答“我是九典AI助手”" not in prompt
    assert CUSTOM_PROMPT_HEADER in prompt


def test_is_custom_system_prompt_excludes_default_placeholder():
    assert is_custom_system_prompt(None) is False
    assert is_custom_system_prompt("") is False
    assert is_custom_system_prompt("   ") is False
    assert is_custom_system_prompt(DEFAULT_SYSTEM_PROMPT_PLACEHOLDER) is False
    assert is_custom_system_prompt("你是采购合规助手。") is True


def test_missing_runtime_flag_falls_back_to_content_check():
    """运行时标志缺失时的兜底路径：退回按 system_prompt 内容判定。

    生产链路始终经 build_agent_input_context 写入 system_prompt_is_custom，
    不会走到这里；兜底分支只能看到追加工作区内容之后的值，因此在「未配置 + 有工作区
    AGENTS.md/USER.md」的组合下会判为已自定义，与主路径结论不同。本用例固定该兜底行为，
    并提醒：新增直接构造 Context 的调用路径时必须自行提供 system_prompt_is_custom。
    """
    prompt = build_prompt_with_context(
        SimpleNamespace(
            workdir_path="/home/gem/user-data/projects/demo",
            system_prompt="用户工作区 agents/AGENTS.md 内容：\n# AGENTS\n",
            # 故意不提供 system_prompt_is_custom，命中回退分支
        )
    )

    assert CUSTOM_PROMPT_HEADER in prompt
    assert "只回答“我是九典AI助手”" not in prompt
