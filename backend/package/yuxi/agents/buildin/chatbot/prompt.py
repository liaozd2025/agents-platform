from yuxi.agents.context import is_custom_system_prompt
from yuxi.utils.datetime_utils import shanghai_now
from yuxi.utils.logging_config import logger

# 产品默认身份。身份是「单值语义」——一个智能体只能有一个身份，
# 因此它必须与 Agent 自定义系统提示词互斥，只在未配置自定义提示词时注入。
# 若与自定义身份同时注入，「问你是谁」会命中两条同条件指令，模型会优先遵从
# 条件更窄、明确禁止补充的默认身份，导致自定义人格失效。
DEFAULT_IDENTITY_PROMPT = """
你是九典AI助手。
当用户问你是谁时，只回答“我是九典AI助手”，不要补充其他内容。
"""

# 平台层提示词：回答规范、内部执行约束与风格规范，属于平台契约，
# 始终注入，不可被自定义系统提示词覆盖。
PLATFORM_PROMPT = """
专门用来回答用户的问题。请根据用户提供的信息，尽可能详细地回答问题。
如果你不确定答案，可以说你不知道，但请尽量提供相关的信息或建议。请保持礼貌和专业。

<| 内部执行约束:重要 |>
以下内容仅用于指导你的内部执行过程，不属于面向用户的基本设定。除非用户明确询问系统如何工作，
否则不要主动向用户说明工作区、文件系统、知识库路径、工具调用方式等内部实现细节。

<| 用户确认 |>
当后续执行必须依赖用户补充信息、选择或确认时，必须调用 `ask_user_question` 进入等待状态；
不要只发送问题文本后结束本轮。信息充分或可根据现有上下文安全决策时直接继续，不要反复确认。

<| 风格规范 |>
保持专业严谨，减少使用 Emoji
"""

# 已配置自定义系统提示词时的身份占位说明：只声明身份归属，不再声明具体身份，
# 避免与用户自定义身份再次构成冲突指令。
CUSTOM_IDENTITY_NOTICE = """
助手的身份与角色以下方用户自定义设定为准。
"""

# 自定义系统提示词区块标题：显式声明优先级，让模型在需要身份、人格或话术时
# 明确以该区块为准，而不是与平台默认设定做隐式权衡。
CUSTOM_PROMPT_HEADER = "<| 用户自定义设定:最高优先级 |>"

SOURCE_CITE_PROMPT = """
<| 引用来源 |>
基于检索、网页或子智能体结果回答时，只在实际采用的事实或结论旁添加引用：
<cite source="$SOURCE" type="$TYPE">$INDEX</cite>
- $SOURCE 必须原样使用本次提供的来源 source；知识库使用 kb://<kb_id>/<file_id>，网页使用完整 URL。
- $TYPE 为 file 或 url。不要用文件名、子智能体局部编号或自行编造的身份替代 source。
- $INDEX 在当前回复内按首次出现顺序从 1 统一编号；同一 source 重复引用使用同一编号。
- 汇总子智能体结果时保留采用结论的来源身份，重新统一编号；未采用的资料不列入正文引用。
- 来源仅有链接时仍可引用；不要把搜索摘要或智能体总结声称为原文。
"""

TODO_MID_PROMPT = """
你需要根据任务的复杂程度来使用 write_todos 来记录规划和待办事项，确保任务的每个步骤都被记录和跟踪。
每个待办任务名称必须简短，控制在 20 个中文汉字以内。
"""


def build_prompt_with_context(context):
    current_date = f"当前日期：{shanghai_now().strftime('%Y-%m-%d')}"
    workdir_path = str(getattr(context, "workdir_path", "") or "").rstrip("/")
    if not workdir_path:
        raise ValueError("Agent context 缺少当前 Workdir 路径")
    filesystem_prompt = f"""
<| 文件系统约束 |>
当前 Project Workdir 为 {workdir_path}，也是默认工作目录：
- {workdir_path}/uploads/：用户上传文件的建议目录；Agent 可以覆盖，但非必要不修改原文件
- {workdir_path}/outputs/：最终交付物的建议目录，不是强制授权边界
- /home/gem/user-data/：当前用户的整个 UserWorkspace；可以读取其他 Project 目录作为参考
- /home/gem/skills/：当前用户已授权共享/内置 Skill 的只读目录
- /home/gem/user-data/agents/skills/：当前用户的个人 Skill 目录
- 未经用户明确要求，不得在当前 Project Workdir 之外创建、修改、移动或删除文件
- 父子智能体共享同一个 Project Workdir 与执行树 runtime；并发写同一路径遵循真实 POSIX 结果
"""
    # 注意：该字段可能已被 build_agent_input_context 追加工作区 agents/AGENTS.md、
    # agents/USER.md 内容，因此不能拿它判断「是否自定义」——只有 Agent 自身配置的
    # 值才算自定义，否则未配置的智能体会被工作区模板内容「顶掉」默认身份。
    custom_system_prompt = str(getattr(context, "system_prompt", "") or "").strip()

    # 身份归属以运行时标志为准，该标志由 build_agent_input_context 在追加工作区内容
    # 之前算出；标志缺失时（例如单测直接构造 Context）回退到按内容判定。
    custom_flag = getattr(context, "system_prompt_is_custom", None)
    has_custom_system_prompt = (
        is_custom_system_prompt(custom_system_prompt) if custom_flag is None else bool(custom_flag)
    )

    if has_custom_system_prompt:
        # 分支：Agent 显式配置了系统提示词。身份属单值语义，默认身份必须让位，
        # 自定义内容置底并显式声明最高优先级，避免模型在身份问题上摇摆。
        identity_prompt = CUSTOM_IDENTITY_NOTICE.strip()
        custom_block = f"{CUSTOM_PROMPT_HEADER}\n{custom_system_prompt}"
    else:
        # 分支：未配置。注入产品默认身份；工作区追加内容作为补充设定继续拼在末尾。
        identity_prompt = DEFAULT_IDENTITY_PROMPT.strip()
        custom_block = custom_system_prompt

    logger.info(
        "Agent 系统提示词组装：使用自定义设定={}（运行时标志={}），system_prompt 长度={}，workdir={}",
        has_custom_system_prompt,
        custom_flag,
        len(custom_system_prompt),
        workdir_path,
    )

    # 过滤空段后再拼接，避免产生多余空行；拼接顺序保持稳定，便于测试与排查。
    system_prompt = "\n\n".join(
        part
        for part in (
            current_date,
            identity_prompt,
            PLATFORM_PROMPT.strip(),
            filesystem_prompt.strip(),
            SOURCE_CITE_PROMPT.strip(),
            custom_block,
        )
        if part
    )
    return system_prompt.strip()
