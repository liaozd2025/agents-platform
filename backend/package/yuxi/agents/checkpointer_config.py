"""LangGraph checkpoint backend 的启动期配置边界。"""

from __future__ import annotations

import os

LANGGRAPH_CHECKPOINTER_BACKEND_ENV = "LANGGRAPH_CHECKPOINTER_BACKEND"
SUPPORTED_CHECKPOINTER_BACKENDS = frozenset({"postgres", "sqlite"})


def resolve_checkpointer_backend(value: str | None = None) -> str:
    """规范并校验 checkpoint backend；未知值必须在启动边界失败。"""

    backend = value if value is not None else os.getenv(LANGGRAPH_CHECKPOINTER_BACKEND_ENV, "postgres")
    normalized = backend.strip().lower()
    if normalized not in SUPPORTED_CHECKPOINTER_BACKENDS:
        raise ValueError(f"不支持的 LangGraph checkpointer backend: {normalized}")
    return normalized
