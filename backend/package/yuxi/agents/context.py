"""Define the configurable parameters for the agent."""

import asyncio
import uuid
from dataclasses import MISSING, dataclass, field, fields
from typing import Any, Literal, get_origin, get_type_hints

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.agents.tool_approval import DEFAULT_TOOL_APPROVAL_MODE
from yuxi.config.options import system_options
from yuxi.utils.logging_config import logger
from yuxi.workspace.filesystem import Workspace

type ResourceSelection = Literal["all"] | list[str]

WORKSPACE_AGENTS_PROMPT_MAX_BYTES = 64 * 1024
WORKSPACE_BASE_CONTEXT_FILES = ("AGENTS.md", "USER.md")
# BaseContext.system_prompt 的默认占位值：它不代表用户配置，只表示「未配置」。
# 判定 Agent 是否显式配置过系统提示词必须以此为准，且只能比较 Agent 自身的配置值；
# 若拿已被追加工作区 AGENTS.md/USER.md 的 system_prompt 去比较，所有智能体都会被
# 误判为「已自定义」，导致产品默认身份丢失、模型无身份可依。
DEFAULT_SYSTEM_PROMPT_PLACEHOLDER = "You are a helpful assistant."


def is_custom_system_prompt(system_prompt: str | None) -> bool:
    """判断 Agent 是否显式配置了系统提示词。

    空值、纯空白与默认占位值都算「未配置」。
    入参必须是 Agent 自身的配置值，不能是已被追加工作区内容后的 system_prompt。
    """
    normalized = str(system_prompt or "").strip()
    return bool(normalized) and normalized != DEFAULT_SYSTEM_PROMPT_PLACEHOLDER


DEFAULT_SUMMARY_THRESHOLD_K = 100  # 100K tokens
DEFAULT_SUMMARY_KEEP_MESSAGES = 10
DEFAULT_SUMMARY_TOOL_RESULT_TOKEN_LIMIT = 300
DEFAULT_MAX_EXECUTION_STEPS = 300
DEFAULT_TOOL_RESULT_EVICTION_K_TOKENS = 3
DEFAULT_YUXI_SUMMARY_PROMPT = """你是对话上下文压缩助手。
你的任务是把下面的对话历史压缩成后续智能体继续工作所需的高价值上下文。

请特别保留并清晰记录：

## SESSION INTENT
用户当前的主要目标、任务范围和最终交付物。

## USER REQUIREMENTS AND PREFERENCES
用户明确提出的要求、偏好、禁忌、输出格式、语言风格、技术约束、验收标准，以及对实现方式的取舍意见。只记录仍然可能影响后续回答或执行的内容。

## PROGRESS AND DECISIONS
已经完成的步骤、关键结论、已确认的方案、被否定的方案及原因。

## ARTIFACTS AND REFERENCES
已经创建、修改、读取或需要继续关注的文件、路径、工具输出路径、线程或运行标识。保留具体路径和关键标识符。

## NEXT STEPS
为了完成用户目标，后续最应该继续做的具体步骤。没有待办时写 None。

要求：
- 不要逐字复述冗长工具输出；保留结论、路径和必要证据。
- 不要编造没有出现在对话中的事实。
- 如果存在未解决的问题或风险，明确记录。
- 使用与用户主要对话一致的语言。

<messages>
{messages}
</messages>

只输出压缩后的上下文，不要添加额外说明。"""


def _load_workspace_agent_context(uid: str) -> str:
    sections: list[str] = []
    filesystem = Workspace(uid)
    for filename in WORKSPACE_BASE_CONTEXT_FILES:
        try:
            content, truncated = filesystem.read_authorized_file_prefix(
                f"/agents/{filename}",
                WORKSPACE_AGENTS_PROMPT_MAX_BYTES,
            )
        except FileNotFoundError:
            continue
        except IsADirectoryError:
            logger.warning(f"读取工作区 {filename} 失败: 路径是目录")
            continue
        except OSError as exc:
            logger.warning(f"读取工作区 {filename} 失败: {exc}")
            continue

        prompt = content[:WORKSPACE_AGENTS_PROMPT_MAX_BYTES].decode("utf-8", errors="replace").strip()
        if not prompt:
            continue
        if truncated:
            prompt = f"{prompt}\n\n[{filename} 内容已截断]"
        sections.append(f"用户工作区 agents/{filename} 内容：\n{prompt}")
    return "\n\n".join(sections)


async def _append_workspace_agent_prompt(context: "BaseContext") -> None:
    """在实际生效的系统提示词后追加工作区基础说明。"""
    workspace_prompt = await asyncio.to_thread(_load_workspace_agent_context, context.uid)
    if workspace_prompt:
        base_prompt = str(context.system_prompt or "").rstrip()
        context.system_prompt = f"{base_prompt}\n\n{workspace_prompt}" if base_prompt else workspace_prompt


def filter_declared_config(
    config_json: dict,
    context_schema: type["BaseContext"] | None = None,
) -> dict:
    """读取持久配置时仅保留 Schema 可配置字段，不按角色修改权限裁剪。"""
    if not isinstance(config_json, dict):
        return {}
    declared_fields = {
        item.name for item in fields(context_schema or BaseContext) if item.metadata.get("configurable", True)
    }
    filtered = dict(config_json)
    context = filtered.get("context")
    if isinstance(context, dict):
        filtered["context"] = {key: value for key, value in context.items() if key in declared_fields}
    return filtered


def filter_agent_config_for_management(
    config_json: dict,
    can_manage: bool,
    context_schema: type["BaseContext"] | None = None,
) -> dict:
    """按智能体管理权限过滤可配置字段。"""
    filtered = filter_declared_config(config_json, context_schema)
    schema = context_schema or BaseContext
    restricted_fields = {item.name for item in fields(schema) if item.metadata.get("manage_only") and not can_manage}

    context = filtered.get("context")
    if isinstance(context, dict):
        filtered["context"] = {key: value for key, value in context.items() if key not in restricted_fields}
    return filtered


@dataclass(kw_only=True)
class BaseContext:
    """
    定义一个基础 Context 供 各类 graph 继承

    配置优先级:
    1. 运行时配置(RunnableConfig)：最高优先级，直接从函数参数传入
    2. 类默认配置：最低优先级，类中定义的默认值
    """

    def update_config(self, data: dict):
        """仅装载允许用户配置的声明字段，运行身份由执行入口注入。"""
        config = filter_declared_config({"context": data}, type(self))["context"]
        for name in self.get_resource_fields().keys() & config.keys():
            config[name] = validate_resource_selection(name, config[name])
        self.update(config)

    def update(self, data: dict):
        """用运行时输入更新已声明的配置字段。"""
        declared_fields = {item.name for item in fields(self)}
        for key, value in data.items():
            if key in declared_fields:
                setattr(self, key, value)

    thread_id: str = field(
        default_factory=lambda: str(uuid.uuid4()),
        metadata={"name": "线程ID", "configurable": False, "description": "用来唯一标识一个对话线程"},
    )

    uid: str = field(
        default_factory=lambda: str(uuid.uuid4()),
        metadata={"name": "UID", "configurable": False, "description": "用来唯一标识一个用户"},
    )

    run_id: str | None = field(
        default=None,
        metadata={"name": "运行 ID", "configurable": False, "hide": True},
    )

    request_id: str | None = field(
        default=None,
        metadata={"name": "请求 ID", "configurable": False, "hide": True},
    )

    worker_id: str | None = field(
        default=None,
        metadata={"name": "Worker Attempt Owner", "configurable": False, "hide": True},
    )

    runtime_scope_id: str | None = field(
        default=None,
        metadata={"name": "Sandbox Runtime Scope", "configurable": False, "hide": True},
    )

    workdir_relative_path: str | None = field(
        default=None,
        metadata={"name": "Workdir Relative Path", "configurable": False, "hide": True},
    )

    workdir_path: str | None = field(
        default=None,
        metadata={"name": "Workdir Virtual Path", "configurable": False, "hide": True},
    )

    system_prompt: str = field(
        default=DEFAULT_SYSTEM_PROMPT_PLACEHOLDER,
        metadata={"name": "系统提示词", "description": "用来描述智能体的角色和行为", "kind": "prompt"},
    )

    system_prompt_is_custom: bool | None = field(
        default=None,
        metadata={"hide": True, "configurable": False},
    )

    model: str = field(
        default="",
        metadata={
            "name": "智能体模型",
            "options": [],
            "description": "智能体的驱动模型，留空时使用系统默认模型。",
            "kind": "llm",
        },
    )

    tool_approval_mode: str = field(
        default=DEFAULT_TOOL_APPROVAL_MODE,
        metadata={
            "name": "工具审批模式",
            "description": "请求审批会在写文件、编辑文件或执行命令前询问；完全信任会自动执行。当前默认完全信任。",
            "options": [
                {"key": "default", "name": "默认审批", "description": "敏感工具执行前请求确认"},
                {"key": "always_trust", "name": "完全信任", "description": "敏感工具无需确认，自动执行"},
            ],
            "type": "string",
            "manage_only": True,
        },
    )

    tools: ResourceSelection = field(
        default="all",
        metadata={
            "name": "工具",
            "description": "内置的工具。默认选择当前用户可用的全部工具。",
            "type": "list",
            "kind": "tools",
        },
    )

    knowledges: ResourceSelection = field(
        default="all",
        metadata={
            "name": "知识库",
            "description": "知识库列表，可以在左侧知识库页面中创建知识库。默认选择当前用户可访问的全部知识库。",
            "type": "list",
            "kind": "knowledges",
        },
    )

    mcps: ResourceSelection = field(
        default_factory=list,
        metadata={
            "name": "MCP服务器",
            "options": [],
            "description": (
                "选择要直接添加到智能体的 MCP 服务器；默认不直接加载，Skill 激活后仍可加载其依赖。"
                "建议使用支持 SSE 的 MCP 服务器，"
                "如果需要使用 uvx 或 npx 运行的服务器，也请在项目外部启动 MCP 服务器，并在项目中配置 MCP 服务器。"
            ),
            "type": "list",
            "kind": "mcps",
        },
    )

    skills: ResourceSelection = field(
        default="all",
        metadata={
            "name": "Skills",
            "options": [],
            "description": "选择共享和内置 Skill，默认全部；个人 Skill 始终可用，无需选择。"
            "Skill 的本地工具和 MCP 依赖在激活后开放；预加载 Skill 从首轮开放依赖。",
            "type": "list",
            "kind": "skills",
        },
    )

    preload_skills: ResourceSelection = field(
        default_factory=list,
        metadata={
            "name": "预加载 Skills",
            "options": [],
            "description": "创建 Agent Graph 时加载完整 Skill 说明，并从首轮开放其依赖工具。"
            "默认不预加载；选择全部时预加载当前已选的共享 Skill。",
            "type": "list",
            "kind": "skills",
        },
    )

    summary_threshold: int = field(
        default=DEFAULT_SUMMARY_THRESHOLD_K,
        metadata={
            "name": "上下文摘要触发阈值 (K)",
            "description": (
                f"当上下文大小超过该值时，启用摘要功能以优化上下文使用。单位为 K，默认值为 "
                f"{DEFAULT_SUMMARY_THRESHOLD_K}K。"
            ),
            "type": "number",
            "manage_only": True,
        },
    )

    summary_keep_messages: int = field(
        default=DEFAULT_SUMMARY_KEEP_MESSAGES,
        metadata={
            "name": "摘要后保留消息数",
            "description": (
                f"上下文摘要触发后，除摘要消息外保留最近的消息数量，默认 {DEFAULT_SUMMARY_KEEP_MESSAGES} 条。"
            ),
            "type": "number",
            "manage_only": True,
        },
    )

    summary_prompt: str = field(
        default=DEFAULT_YUXI_SUMMARY_PROMPT,
        metadata={
            "name": "上下文摘要提示词",
            "description": "触发上下文摘要时使用的提示词，必须能接收 {messages} 作为待摘要消息占位符。",
            "type": "string",
            "kind": "prompt",
            "manage_only": True,
        },
    )

    summary_tool_result_token_limit: int = field(
        default=DEFAULT_SUMMARY_TOOL_RESULT_TOKEN_LIMIT,
        metadata={
            "name": "摘要工具结果 token 上限",
            "description": (
                "确定性压缩历史工具结果时，超过该 token 数的 ToolMessage 会写入 outputs，"
                "并在上下文中保留不超过该 token 数的预览；未超过则保持原样。默认 "
                f"{DEFAULT_SUMMARY_TOOL_RESULT_TOKEN_LIMIT}。"
            ),
            "type": "number",
            "manage_only": True,
        },
    )

    max_execution_steps: int = field(
        default=DEFAULT_MAX_EXECUTION_STEPS,
        metadata={
            "name": "最大执行步数",
            "description": (
                "单次 Agent 运行允许的最大 LangGraph 执行步数，对应 recursion_limit，默认 "
                f"{DEFAULT_MAX_EXECUTION_STEPS}。"
            ),
            "type": "number",
            "manage_only": True,
        },
    )

    model_retry_times: int = field(
        default=2,
        metadata={
            "name": "模型重试次数",
            "description": "模型调用失败时的最大重试次数，默认值为 2。",
            "type": "number",
            "manage_only": True,
        },
    )

    @classmethod
    def get_resource_fields(cls) -> dict[str, str]:
        """从字段类型识别资源选择，并读取声明的候选资源种类。"""
        types = get_type_hints(cls)
        return {
            item.name: item.metadata.get("kind", item.name)
            for item in fields(cls)
            if types[item.name] is ResourceSelection and item.metadata.get("configurable", True)
        }

    @classmethod
    def get_configurable_items(cls, can_manage: bool = False):
        """实现一个可配置的参数列表，在 UI 上配置时使用"""
        configurable_items = {}
        resource_fields = cls.get_resource_fields()
        for f in fields(cls):
            if f.init and not f.metadata.get("hide", False):
                if f.metadata.get("manage_only") and not can_manage:
                    continue
                if f.metadata.get("configurable", True):
                    type_name = cls._get_type_name(f.type)

                    options = f.metadata.get("options", [])
                    if callable(options):
                        options = options()

                    configurable_items[f.name] = {
                        "type": f.metadata.get("type", type_name),
                        "name": f.metadata.get("name", f.name),
                        "options": options,
                        "default": f.default
                        if f.default is not MISSING
                        else f.default_factory()
                        if f.default_factory is not MISSING
                        else None,
                        "description": f.metadata.get("description", ""),
                        "kind": f.metadata.get("kind", ""),
                        "supports_all": f.name in resource_fields,
                    }

        return configurable_items

    @classmethod
    def _get_type_name(cls, field_type) -> str:
        """获取类型名称"""
        origin = get_origin(field_type)
        if origin is not None:
            if hasattr(origin, "__name__"):
                return origin.__name__
            return str(origin)
        elif hasattr(field_type, "__name__"):
            return field_type.__name__
        else:
            return str(field_type)


def _resource_option(key: Any, name: Any = None, description: Any = None) -> dict[str, str]:
    key_value = str(key)
    return {
        "key": key_value,
        "name": str(name or key_value),
        "description": str(description or ""),
    }


async def resolve_agent_resource_options(
    resource_fields: set[str],
    *,
    db,
    user,
) -> dict[str, list[dict[str, str]]]:
    fields_to_load = resource_fields
    if not fields_to_load:
        return {}

    options: dict[str, list[dict[str, str]]] = {}

    if "tools" in fields_to_load:
        from yuxi.agents.toolkits.service import get_tool_metadata

        options["tools"] = [
            _resource_option(tool["slug"], tool.get("name"), tool.get("description"))
            for tool in get_tool_metadata(category="buildin")
            if tool.get("slug")
        ]
    if "knowledges" in fields_to_load:
        from yuxi.knowledge.runtime import knowledge_base
        from yuxi.permissions.authorization import build_authorization_context

        databases = (
            await knowledge_base.get_databases_by_user(user)
            if build_authorization_context(user).has_permission("knowledge_base:read")
            else []
        )
        options["knowledges"] = [
            _resource_option(item.kb_id, item.name, item.description) for item in databases if item.kb_id
        ]
    if "mcps" in fields_to_load:
        from yuxi.agents.mcp.service import get_all_mcp_servers, get_enabled_mcp_server_slugs

        servers = await get_all_mcp_servers(db)
        enabled_slugs = set(await get_enabled_mcp_server_slugs(db=db))
        options["mcps"] = [
            _resource_option(server.slug, server.name, server.description)
            for server in servers
            if server.slug in enabled_slugs
        ]
    if "skills" in fields_to_load:
        from yuxi.repositories.skill_repository import SkillRepository
        from yuxi.permissions.authorization import build_authorization_context

        skills = (
            await SkillRepository(db).list_enabled_readable(user)
            if build_authorization_context(user).has_permission("skill:use")
            else []
        )
        options["skills"] = [
            _resource_option(skill.slug, skill.name, skill.description) for skill in skills if skill.slug
        ]
    if "subagents" in fields_to_load:
        from yuxi.repositories.agent_repository import AgentRepository

        subagents = await AgentRepository(db).list_visible_subagents(user=user)
        options["subagents"] = [
            _resource_option(agent.slug, agent.name, agent.description) for agent in subagents if agent.slug
        ]

    return options


async def normalize_agent_context_config(
    context: dict | None,
    *,
    db,
    user,
    context_schema: type[BaseContext] | None = None,
) -> dict:
    """按字段默认值展开资源选择，返回当前用户可用的运行列表。"""
    schema = context_schema or BaseContext
    raw_context = dict(context) if isinstance(context, dict) else {}
    filtered = filter_declared_config({"context": raw_context}, schema)
    defaults = schema()
    normalized = dict(filtered.get("context") or {})
    resource_fields = schema.get_resource_fields()
    for field_name in resource_fields:
        normalized[field_name] = validate_resource_selection(
            field_name, normalized.get(field_name, getattr(defaults, field_name))
        )
    fields_to_load = {kind for name, kind in resource_fields.items() if name != "preload_skills" and normalized[name]}
    if fields_to_load:
        resource_options = await resolve_agent_resource_options(fields_to_load, db=db, user=user)
        for name, kind in resource_fields.items():
            if name != "preload_skills" and normalized[name]:
                normalized[name] = _resolve_resource_selection(
                    normalized[name], [option["key"] for option in resource_options[kind]]
                )

    if "preload_skills" in resource_fields:
        normalized["preload_skills"] = _resolve_resource_selection(
            normalized["preload_skills"], normalized.get("skills", [])
        )

    return normalized


async def prepare_agent_runtime_context(
    context: BaseContext,
) -> BaseContext:
    """为单次运行解析资源与 Skill；同一对象再次构图时复用准备结果。"""
    if getattr(context, "_runtime_prepared", False):
        return context
    schema = type(context)
    resource_fields = schema.get_resource_fields()
    uid = str(getattr(context, "uid", "") or "").strip()
    if not uid:
        return context

    from yuxi.agents.skills.runtime import resolve_runtime_skills_for_context
    from yuxi.repositories.user_repository import UserRepository
    from yuxi.storage.postgres.manager import pg_manager

    async with pg_manager.get_async_session_context() as db:
        from yuxi.services.user_memory_service import sync_user_profile_to_memory

        try:
            async with db.begin_nested():
                await sync_user_profile_to_memory(db=db, uid=uid)
        except Exception:
            logger.exception("用户资料同步失败，沿用工作区内容：uid={}，thread_id={}", uid, context.thread_id)
        context.system_prompt_is_custom = is_custom_system_prompt(context.system_prompt)
        await _append_workspace_agent_prompt(context)
        if not _is_model_spec_resolvable(getattr(context, "model", "")):
            if context.run_id and str(context.model or "").strip():
                raise ValueError(f"运行绑定的聊天模型已不可用: {context.model}")
            fallback_spec = await resolve_available_default_chat_model(db)
            if fallback_spec:
                setattr(context, "model", fallback_spec)
        user = await UserRepository().get_by_uid_with_db(db, uid)
        if user is None:
            for field_name in resource_fields:
                setattr(context, field_name, [])
            context._skill_runtime_snapshot = {}
            setattr(context, "_visible_knowledge_bases", [])
            return context

        raw_resources = {field_name: getattr(context, field_name, None) for field_name in resource_fields}
        normalized = await normalize_agent_context_config(
            raw_resources,
            db=db,
            user=user,
            context_schema=schema,
        )
        for field_name in resource_fields:
            setattr(context, field_name, normalized[field_name])

        from yuxi.agents.backends.knowledge_base_backend import resolve_visible_knowledge_bases_for_context

        await resolve_visible_knowledge_bases_for_context(context)
        skill_scope = await resolve_runtime_skills_for_context(context, db=db, user=user)
        setattr(context, "_skill_runtime_snapshot", skill_scope)
        context.skills = skill_scope["context_skills"]
        context.preload_skills = skill_scope["context_preload_skills"]

    context._runtime_prepared = True

    return context


def _is_model_spec_resolvable(model_spec: Any) -> bool:
    """判断模型 spec 当前是否还能被解析为可用的对话模型。

    空值与「供应商已下架」都视为不可解析：模型下架后模型缓存中不再有该 spec，
    继续沿用会让下游 load_chat_model 抛 Unknown model spec。
    非 chat 类型（embedding / rerank）同样视为不可解析——放进对话上下文没有意义。
    """
    spec = str(model_spec or "").strip()
    if not spec:
        return False

    from yuxi.models.providers.cache import model_cache

    info = model_cache.get_model_info(spec)
    return info is not None and getattr(info, "model_type", None) == "chat"


async def resolve_available_default_chat_model(db: AsyncSession | None = None) -> str:
    """取一个当前确实可用的对话模型 spec。

    优先使用系统配置的默认对话模型；当它本身已被供应商下架时，退回到可用列表首项，
    保证运行时上下文总能拿到一个能解析的模型。全部不可用时返回空字符串，由调用方决定
    失败方式（不静默塞入非法值）。
    """
    from yuxi.models.providers.cache import model_cache

    configured = str((await system_options.get(db))["default_model"] or "").strip()
    if _is_model_spec_resolvable(configured):
        return configured

    if configured:
        logger.warning(f"系统默认对话模型 {configured} 已不可用，回落到可用模型列表首项")

    for info in model_cache.get_all_specs("chat"):
        spec = getattr(info, "spec", "") or ""
        if spec:
            logger.warning(f"改用可用对话模型 {spec} 作为运行时默认模型")
            return spec

    logger.error("没有可用的对话模型，运行时上下文将缺少模型设置")
    return ""


def validate_resource_selection(field_name: str, value: Any) -> ResourceSelection:
    """统一校验全部或显式列表的资源选择协议。"""
    if value == "all":
        return "all"
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f'智能体资源字段 {field_name} 必须是"all" 或非空字符串组成的列表')
    return list(dict.fromkeys(item.strip() for item in value))


def _resolve_resource_selection(selection: ResourceSelection, available: list[str]) -> list[str]:
    """从调用方提供的候选范围解析全部或指定选择。"""
    if selection == "all":
        return list(available)
    allowed = set(available)
    return [key for key in selection if key in allowed]
