"""将当前用户资料同步到受隔离的 Agent 用户记忆文件。"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.repositories.user_repository import UserRepository
from yuxi.workspace.filesystem import Workspace
from yuxi.workspace.paths import ensure_user_workspace

PROFILE_START = "<!-- YUXI_USER_PROFILE_START -->"
PROFILE_END = "<!-- YUXI_USER_PROFILE_END -->"
PROFILE_PATH = "/agents/USER.md"


async def sync_user_profile_to_memory(*, db: AsyncSession, uid: str) -> None:
    """按数据库资料更新 USER.md，保留手工内容与工作区路径隔离。"""
    row = await UserRepository(db).get_memory_profile(uid)
    if row is None:
        return
    username, department_name, enable_memory, roles = row
    profile = None
    if enable_memory:
        profile = "\n".join(
            (
                PROFILE_START,
                "## 用户资料",
                f"- 用户名：{username}",
                f"- UID：{uid}",
                f"- 部门：{department_name or '未分配'}",
                f"- 角色：{'、'.join(roles) or '未分配'}",
                PROFILE_END,
            )
        )
    await asyncio.to_thread(_update_profile_file, uid, profile)


def _update_profile_file(uid: str, profile: str | None) -> None:
    """完整读取后原子替换，超限或不安全路径失败时保持原文件。"""
    ensure_user_workspace(uid)
    workspace = Workspace(uid)
    try:
        content = workspace.read_authorized_file(PROFILE_PATH, 1024 * 1024).decode("utf-8")
    except FileNotFoundError:
        content = ""
    updated = _replace_profile_block(content, profile) if profile is not None else _remove_profile_block(content)
    if updated != content:
        workspace.replace_authorized_file(PROFILE_PATH, updated.encode("utf-8"))


def _replace_profile_block(content: str, profile: str) -> str:
    """替换机器维护区块，保留区块外的手工内容。"""
    start = content.find(PROFILE_START)
    end = content.find(PROFILE_END)
    if start >= 0 and end >= start:
        end += len(PROFILE_END)
        prefix = content[:start].rstrip()
        suffix = content[end:].lstrip()
        return "\n\n".join(part for part in (prefix, profile, suffix) if part) + "\n"

    base = content.rstrip()
    return f"{base}\n\n{profile}\n" if base else f"{profile}\n"


def _remove_profile_block(content: str) -> str:
    """移除机器维护区块，避免关闭 Memory 后继续注入旧资料。"""
    start = content.find(PROFILE_START)
    end = content.find(PROFILE_END)
    if start < 0 or end < start:
        return content
    end += len(PROFILE_END)
    prefix = content[:start].rstrip()
    suffix = content[end:].lstrip()
    return "\n\n".join(part for part in (prefix, suffix) if part) + ("\n" if prefix or suffix else "")
