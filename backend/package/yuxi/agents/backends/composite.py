from __future__ import annotations

from dataclasses import dataclass

from deepagents.backends import CompositeBackend
from deepagents.middleware.filesystem import (
    TOOLS_EXCLUDED_FROM_EVICTION,
    FilesystemMiddleware,
)
from langchain_core.messages import ToolMessage

from yuxi.agents.backends.paths import (
    VIRTUAL_PERSONAL_SKILLS_PATH,
    VIRTUAL_SKILLS_PATH,
    runtime_workdir_path,
)
from yuxi.agents.skills.service import refresh_user_skill_projection_async
from yuxi.agents.tool_approval import PI_DELEGATED_SANDBOX_TOOLS

from .sandbox import ProvisionerSandboxBackend

# Yuxi 在 DeepAgents 内建排除集之上额外豁免知识库文档工具结果，
# 避免 read_file/offload 循环：该工具自带分页与引用语义。
_TOOL_RESULT_EVICTION_EXEMPT_TOOLS = frozenset(TOOLS_EXCLUDED_FROM_EVICTION) | {"open_kb_document"}

# 文件工具 allowlist：显式排除 destructive delete。Yuxi backend 未实现 delete，
# 且删除语义需要审批与审计设计，开放前不应让模型看到该工具。
_PI_REDIRECTED_TOOLS = PI_DELEGATED_SANDBOX_TOOLS | {"read_file"}


class YuxiFilesystemMiddleware(FilesystemMiddleware):
    """只允许模型通过 read_file 读取只读 Skill 投影。"""

    @staticmethod
    def _is_skill_read(request) -> bool:
        tool_call = request.tool_call if isinstance(request.tool_call, dict) else {}
        if tool_call.get("name") != "read_file":
            return False
        args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
        file_path = str(args.get("file_path") or "").strip()
        return any(
            file_path.startswith(f"{root.rstrip('/')}/") for root in (VIRTUAL_SKILLS_PATH, VIRTUAL_PERSONAL_SKILLS_PATH)
        )

    @staticmethod
    def _sandbox_redirect(request) -> ToolMessage:
        tool_call = request.tool_call if isinstance(request.tool_call, dict) else {}
        return ToolMessage(
            "用户沙箱文件必须通过 pi_sandbox 交给 PI Agent 处理。",
            tool_call_id=str(tool_call.get("id") or ""),
        )

    def wrap_tool_call(self, request, handler):
        if request.tool_call["name"] in _PI_REDIRECTED_TOOLS and not self._is_skill_read(request):
            return self._sandbox_redirect(request)
        tool_result = handler(request)

        if request.tool_call["name"] in _TOOL_RESULT_EVICTION_EXEMPT_TOOLS:
            return tool_result
        if self._tool_token_limit_before_evict is None:
            return tool_result

        return self._intercept_large_tool_result(tool_result)

    async def awrap_tool_call(self, request, handler):
        if request.tool_call["name"] in _PI_REDIRECTED_TOOLS and not self._is_skill_read(request):
            return self._sandbox_redirect(request)
        tool_result = await handler(request)

        if request.tool_call["name"] in _TOOL_RESULT_EVICTION_EXEMPT_TOOLS:
            return tool_result
        if self._tool_token_limit_before_evict is None:
            return tool_result

        return await self._aintercept_large_tool_result(tool_result)


@dataclass(frozen=True)
class _BackendScope:
    runtime_scope_id: str
    workdir_relative_path: str
    uid: str

    @property
    def workdir_path(self) -> str:
        return runtime_workdir_path(self.workdir_relative_path)

    @classmethod
    def from_sources(cls, *sources, error_context: str) -> _BackendScope:
        def string_value(key: str) -> str | None:
            for source in sources:
                value = source.get(key) if isinstance(source, dict) else getattr(source, key, None)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        thread_id = string_value("thread_id")
        if not thread_id:
            raise ValueError(f"thread_id is required in {error_context}")

        uid = string_value("uid")
        if not uid:
            raise ValueError(f"uid is required in {error_context}")

        runtime_scope_id = string_value("runtime_scope_id") or thread_id
        relative_path = string_value("workdir_relative_path") or ""
        return cls(
            runtime_scope_id=runtime_scope_id,
            workdir_relative_path=relative_path,
            uid=uid,
        )

    def create_backend(self) -> CompositeBackend:
        if not self.workdir_relative_path:
            raise ValueError("workdir path is required in runtime context")
        # artifacts_root 指向 outputs 目录：Filesystem/Summarization middleware 由此
        # 派生 large_tool_results 与 conversation_history 前缀，与 Yuxi 契约一致。
        return CompositeBackend(
            default=ProvisionerSandboxBackend(
                thread_id=self.runtime_scope_id,
                uid=self.uid,
                workdir_path=self.workdir_relative_path,
                create_if_missing=False,
            ),
            routes={},
            artifacts_root=f"{self.workdir_path.rstrip('/')}/outputs",
        )


async def sync_agent_context_skills(context) -> None:
    """在 Agent Run 初始化时同步当前用户获授权的共享 Skill 投影。"""
    scope = _BackendScope.from_sources(context, error_context="runtime context")
    await refresh_user_skill_projection_async(scope.uid)


def create_agent_composite_backend(context) -> CompositeBackend:
    """按已准备的 Agent context 构造本 Run 独享的 CompositeBackend 实例。

    DeepAgents 0.7 移除了 backend factory：每次 graph 构造时基于 context 创建
    具体实例，并由 filesystem 与 summary middleware 共用同一实例，保持
    user/thread/file_thread 的隔离边界。
    """
    return _BackendScope.from_sources(context, error_context="agent context").create_backend()


def create_agent_filesystem_middleware(
    tool_token_limit_before_evict: int | None = None,
    *,
    backend: CompositeBackend,
) -> FilesystemMiddleware:
    return YuxiFilesystemMiddleware(
        backend=backend,
        tool_token_limit_before_evict=tool_token_limit_before_evict,
        tools=["read_file"],
    )
