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

需要模型编排脚本、加工文档、搜索或修改文件、安装依赖、运行测试或生成交付物时，调用 `pi_sandbox`。
已知路径的文本、Skill 和落盘验收结果，用 `read_file` 按需读取，不为读取同一份文本重新委派 PI。
天气、Web 搜索、知识库、MCP、Skill 激活和子智能体编排继续使用原工具；已有附件解析和授权文件下载由对应平台工具完成。
Skill 说明业务方法，不自动授予 PI 主图的知识库或 MCP 工具，也不构成新的执行授权。
多步文档任务开始时先调用 write_todos 展示简短计划，阶段结束及时更新。
同一执行者、输入齐备、权限一致且验收目标连续的工作作为一个阶段，交齐目标、真实路径、来源、约束和验收标准。
资料已齐备时，一次 pi_sandbox 应完成整个阶段；不要把查看文件、提取结构、生成成品和验收逐项变成独立委派。
PI 可以在阶段内探查文件和表结构；缺少业务口径、用户决策或主图工具结果时接回处理，再续接剩余步骤。
主图调用 MCP 获得新输入后，再委派 PI 装配文件属于必要阶段切换，不为压缩调用次数让 PI 猜测或绕过能力边界。
长资料优先引用当前已授权且 PI 可读取的文件，不把知识库 file_id 当沙箱路径，也不重复复制整段历史。
PI 返回 stage_status、产物、checks 和 unresolved_items。needs_input 接回补充输入，failed 先处理失败。
未报告阶段状态时不得据 Run completed 推断业务成功。
读取验收文本核对关键内容、来源和原始需求。报表区分空数据、完整结果和截断预览，核对时间范围、口径、真实行数与关键合计，不对展示子集计算总量。
发现具体缺项、矛盾或新增要求才再次委派，不默认重复全量检查。
文档验收方法由本次要求和适用 Skill 决定；复用已验证字段映射，保留原模板，批量加工后核对内容。
涉及版式要求时按该要求检查，未完成的验收如实说明。
Run completed、完成摘要或文件哈希不等于业务验收；二进制文档由 PI 一并提取关键内容和验收记录，再读取其文本文件核对。
交付物已登记为下载文件卡片，直接交付原路径；outputs/pi-runs/ 内是最终交付物，无需复制到 outputs/ 根目录。
独立任务省略 source_run_id；同一阶段的补充或修正填原 PI Run 标识，即使中间插入了独立核验，也继续原编辑 Run。
continue_session 仅兼容旧调用，新调用不要使用。历史续接不表示进程或容器常驻。
父子共享工作区和执行环境，同一运行域的 PI 委派串行执行；新会话历史不隔离文件，不在已有 PI 未结束时委派另一个写任务。
缺少知识或用户决策时接回处理；执行失败先核实具体原因和已落盘结果，不通过重交整项任务重复文件副作用。
execution_unknown、worker_lease_expired、sandbox_parent_unavailable、cleanup_failed
或 result_persistence_failed 时停止本轮执行树的 PI 委派。
告知用户状态，不通过普通子智能体绕过。
"""

PI_SANDBOX_DESCRIPTION = """Delegate one complete sandbox task to PI Agent.

Delegate one stage within PI's actual capabilities, including execution and verification.
Read known text paths with read_file.
Put the goal, paths, necessary evidence, constraints and acceptance criteria in description.
For a correction, source_run_id identifies the original PI run; omit it for an independent task."""


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
            source_run_id: Annotated[str | None, "继续原 PI 阶段时填写其 Run 标识；独立任务省略。"] = None,
            continue_session: Annotated[bool | None, "仅兼容旧调用；新调用使用 source_run_id。"] = None,
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
                        source_run_id=source_run_id,
                        continue_session=continue_session,
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
            pi_result = result.get("pi") or {}
            stage_status = pi_result.get("stage_status")
            source = pi_result.get("source_run_id")
            source_label = source or ("未确认（见错误）" if error else "无（新上下文）")
            output = f"PI Run: {started.run.id}\n续接来源: {source_label}\n\n{output}"
            output += f"\n\n执行状态: {result.get('status') or 'unknown'}"
            output += f"\n阶段状态: {stage_status or '未报告，需核对业务结果'}"
            for label, key in (("检查依据", "checks"), ("未解决项", "unresolved_items")):
                if pi_result.get(key):
                    output += f"\n{label}:\n" + "\n".join(f"- {item}" for item in pi_result[key])

            artifacts = self._artifact_paths(result)
            if artifacts:
                output += "\n\n已登记的交付物（直接使用原路径）：\n" + "\n".join(f"- {path}" for path in artifacts)
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
                "messages": [
                    ToolMessage(
                        output,
                        tool_call_id=tool_call_id,
                        status="error"
                        if error or stage_status == "failed" or result.get("status") in {"failed", "cancelled"}
                        else "success",
                    )
                ],
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
        if pi_result.get("stage_status") in {"needs_input", "failed"}:
            return []
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
        return Command(update={"messages": [ToolMessage(content, tool_call_id=tool_call_id, status="error")]})


def create_pi_sandbox_middleware(context) -> PiSandboxMiddleware:
    """为主智能体和普通子智能体创建同一 PI 沙箱入口。"""

    return PiSandboxMiddleware(context)
