"""把主、子 Agent 的完整沙箱任务委派给 PI child Run。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, TypeVar

from deepagents.middleware._utils import append_to_system_message
from fastapi import HTTPException
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from yuxi.agents.backends.paths import VIRTUAL_PERSONAL_SKILLS_PATH, VIRTUAL_SKILLS_PATH
from yuxi.agents.skills.service import (
    get_personal_skills_root_dir,
    get_user_skills_root_dir,
    normalize_string_list,
)
import yuxi.services.agent_run_service as agent_run_service
from yuxi.services.pi_sandbox_run_service import PiSandboxRunService, execute_pi_sandbox_run
from yuxi.services.subagent_run_service import subagent_run_urls
from yuxi.storage.postgres.manager import pg_manager

ContextT = TypeVar("ContextT")
ResponseT = TypeVar("ResponseT")

PI_SANDBOX_PROMPT = """## `pi_sandbox`（PI Agent 沙箱）

凡是需要查看、列举、搜索、读取、创建或修改沙箱文件，执行命令、安装依赖、运行测试或生成交付物，都必须调用 `pi_sandbox`。
不要尝试使用其它文件或命令工具访问 workspace、uploads、outputs。
天气、Web 搜索、知识库、MCP、Skill 激活和子智能体编排继续使用原工具。
调用时在 `description` 中一次给出完整目标、已知路径、上下文和验收标准。
"""

PI_SANDBOX_DESCRIPTION = """Delegate one complete sandbox task to PI Agent.

Use it for every sandbox file read/list/search/write/edit operation, command, dependency install, test, or deliverable.
Put the complete task brief, known paths, context, and acceptance criteria in description."""


class PiSandboxMiddleware(AgentMiddleware[Any, ContextT, ResponseT]):
    """向 LangGraph 暴露唯一的用户沙箱任务入口。"""

    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.tools = [self._build_tool()]

    def wrap_model_call(self, request: ModelRequest[ContextT], handler: Callable) -> ModelResponse[ResponseT]:
        return handler(
            request.override(system_message=append_to_system_message(request.system_message, PI_SANDBOX_PROMPT))
        )

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        return await handler(
            request.override(system_message=append_to_system_message(request.system_message, PI_SANDBOX_PROMPT))
        )

    def _build_tool(self) -> StructuredTool:
        async def pi_sandbox(
            description: Annotated[str, "PI Agent 要完成的完整沙箱任务、上下文和验收标准。"],
            runtime: ToolRuntime,
        ) -> Command:
            tool_call_id = str(runtime.tool_call_id or "").strip()
            uid = str(getattr(self.context, "uid", "") or "").strip()
            parent_run_id = str(getattr(self.context, "run_id", "") or "").strip()
            if not tool_call_id or not uid or not parent_run_id:
                return self._message(tool_call_id, "无法启动 PI 沙箱：当前运行上下文不完整")

            try:
                skill_slugs, skill_sources, skill_runtime_paths = self._selected_skills(uid)
                async with pg_manager.get_async_session_context() as db:
                    started = await PiSandboxRunService(db).start(
                        uid=uid,
                        created_by_run_id=parent_run_id,
                        description=description,
                        tool_call_id=tool_call_id,
                        skill_slugs=skill_slugs,
                        skill_sources=skill_sources,
                        skill_runtime_paths=skill_runtime_paths,
                    )
                await execute_pi_sandbox_run(started.run.id)
                result = await agent_run_service.load_agent_run_result(run_id=started.run.id, current_uid=uid)
            except (ValueError, HTTPException) as exc:
                detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                return self._message(tool_call_id, f"PI 沙箱启动失败：{detail}")

            output = str(result.get("output") or "").strip()
            error = result.get("error") if isinstance(result.get("error"), dict) else None
            if not output and error:
                output = str(error.get("message") or "PI 沙箱运行失败")
            if not output:
                output = f"PI 沙箱任务状态：{result.get('status') or 'unknown'}"

            artifacts = self._artifact_paths(result)
            if artifacts:
                output += "\n\n交付物：\n" + "\n".join(f"- {path}" for path in artifacts)
            child_thread_id = str(result.get("thread_id") or started.run.conversation_thread_id)
            subagent_run = {
                "id": tool_call_id,
                "run_id": started.run.id,
                "subagent_slug": "pi_sandbox",
                "subagent_name": "PI Agent",
                "child_thread_id": child_thread_id,
                "status": str(result.get("status") or "failed"),
                **subagent_run_urls(started.run.id),
            }
            if (result.get("pi") or {}).get("stop_reason") == "steer":
                subagent_run["stop_reason"] = "steer"
            if error:
                subagent_run["error"] = str(error.get("message") or "PI 沙箱运行失败")
            update = {
                "messages": [ToolMessage(output, tool_call_id=tool_call_id)],
                "subagent_runs": [subagent_run],
            }
            if artifacts:
                update["artifacts"] = artifacts
            return Command(update=update)

        return StructuredTool.from_function(
            name="pi_sandbox",
            coroutine=pi_sandbox,
            description=PI_SANDBOX_DESCRIPTION,
            infer_schema=True,
        )

    def _selected_skills(self, uid: str) -> tuple[list[str], dict[str, Path], dict[str, str]]:
        """把当前 Agent 的 Skill 快照解析为宿主来源与沙箱路径。"""

        slugs = normalize_string_list(getattr(self.context, "_effective_skill_slugs", []) or [])
        runtime_skills = dict(getattr(self.context, "_runtime_skills", {}) or {})
        shared_root = get_user_skills_root_dir(uid)
        personal_root = get_personal_skills_root_dir(uid)
        sources: dict[str, Path] = {}
        runtime_paths: dict[str, str] = {}
        for slug in slugs:
            runtime_path = str((runtime_skills.get(slug) or {}).get("path") or "")
            skill_root = str(PurePosixPath(runtime_path).parent)
            if skill_root == f"{VIRTUAL_SKILLS_PATH}/{slug}":
                source = shared_root / slug
            elif skill_root == f"{VIRTUAL_PERSONAL_SKILLS_PATH}/{slug}":
                source = personal_root / slug
            else:
                raise ValueError(f"PI 沙箱无法解析 Skill 路径: {slug}")
            sources[slug] = source
            runtime_paths[slug] = skill_root
        return slugs, sources, runtime_paths

    def _artifact_paths(self, result: dict) -> list[str]:
        pi_result = result.get("pi") if isinstance(result.get("pi"), dict) else {}
        output_subdir = str(pi_result.get("output_subdir") or "").strip()
        artifact = pi_result.get("artifact") if isinstance(pi_result.get("artifact"), dict) else {}
        files = artifact.get("files") if isinstance(artifact.get("files"), list) else []
        workdir_path = str(getattr(self.context, "workdir_path", "") or "").rstrip("/")
        subdir = PurePosixPath(output_subdir)
        if (
            not workdir_path
            or subdir.is_absolute()
            or len(subdir.parts) != 2
            or subdir.parts[0] != "pi-runs"
            or ".." in subdir.parts
        ):
            return []
        paths = []
        for item in files:
            relative = PurePosixPath(str(item.get("path") or "")) if isinstance(item, dict) else PurePosixPath()
            if not relative.parts or relative.is_absolute() or ".." in relative.parts:
                continue
            paths.append(f"{workdir_path}/outputs/{subdir.as_posix()}/{relative.as_posix()}")
        return paths

    @staticmethod
    def _message(tool_call_id: str, content: str) -> Command:
        return Command(update={"messages": [ToolMessage(content, tool_call_id=tool_call_id)]})


def create_pi_sandbox_middleware(context) -> PiSandboxMiddleware:
    """为主智能体和普通子智能体创建同一 PI 沙箱入口。"""

    return PiSandboxMiddleware(context)
