from pathlib import PurePosixPath
from typing import Literal

from langchain.agents.middleware import HumanInTheLoopMiddleware

ToolApprovalMode = Literal["default", "always_trust"]

# 系统级工具审批默认模式：请求与 Agent 配置都未指定时生效。当前默认完全信任（敏感工具自动执行）。
DEFAULT_TOOL_APPROVAL_MODE: ToolApprovalMode = "always_trust"
TOOL_APPROVAL_MODES = frozenset({"default", "always_trust"})
SANDBOX_EXECUTION_TOOLS = frozenset({"write_file", "edit_file", "execute"})
PI_DELEGATED_SANDBOX_TOOLS = SANDBOX_EXECUTION_TOOLS | {"ls", "glob", "grep", "ocr_parse_file"}
# 默认审批模式下需要拦截或对子 Agent 隐藏的敏感 backend 工具。
SENSITIVE_BACKEND_TOOLS = SANDBOX_EXECUTION_TOOLS
_ALLOWED_DECISIONS = ["approve", "reject"]


def normalize_tool_approval_mode(value: object) -> ToolApprovalMode:
    mode = value.strip() if isinstance(value, str) else value
    if mode not in TOOL_APPROVAL_MODES:
        raise ValueError(f"不支持的 tool_approval_mode: {value}")
    return mode


def require_pi_delegation_approval(*, creator_run_type: str, tool_approval_mode: object) -> None:
    """普通子图没有审批恢复入口，需将受审批的 PI 委派交回主图。"""
    mode = normalize_tool_approval_mode(tool_approval_mode)
    if creator_run_type == "subagent" and mode == "default":
        raise ValueError("当前子智能体不能批准 PI 执行，请将任务交回主智能体，由主图批准 pi_sandbox 后执行")


def create_tool_approval_middleware(
    mode: ToolApprovalMode,
    *,
    current_project_path: str | None = None,
):
    """按审批模式与当前 Project 构造敏感工具审批。"""
    if mode == "always_trust":
        return None

    write_requires_approval = _project_write_requires_approval(current_project_path or "")
    return HumanInTheLoopMiddleware(
        interrupt_on={
            "write_file": {"allowed_decisions": _ALLOWED_DECISIONS, "when": write_requires_approval},
            "edit_file": {"allowed_decisions": _ALLOWED_DECISIONS, "when": write_requires_approval},
            "execute": {"allowed_decisions": _ALLOWED_DECISIONS},
            "pi_sandbox": {"allowed_decisions": _ALLOWED_DECISIONS},
        }
    )


def _project_write_requires_approval(current_project_path: str):
    """创建仅豁免当前 Project 写入的审批谓词。"""
    project_root = _normalize_runtime_path(current_project_path)

    def requires_approval(request) -> bool:
        """非法路径或 Project 外路径继续请求人工确认。"""
        args = request.tool_call.get("args")
        file_path = _normalize_runtime_path(args.get("file_path") if isinstance(args, dict) else None)
        if project_root is None or file_path is None:
            return True
        return file_path != project_root and project_root not in file_path.parents

    return requires_approval


def _normalize_runtime_path(value: object) -> PurePosixPath | None:
    """将审批参数收敛为无跳转的 Sandbox 绝对路径。"""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    path = PurePosixPath(raw)
    if not raw or not path.is_absolute() or ".." in path.parts or "\\" in raw or "://" in raw:
        return None
    return path
