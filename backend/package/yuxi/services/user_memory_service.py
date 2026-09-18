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
# 历史版本自动生成的文件头；同步时升级为「关于我」，只匹配完全一致的生成段落
LEGACY_PREAMBLE = "# USER\n\n以下是有关用户的一些信息\n"
PREFERRED_PREAMBLE = "# 关于我\n\n"


def _upgrade_legacy_preamble(content: str) -> str:
    """把历史自动生成的文件头升级为「关于我」，手工内容不受影响。"""
    if content.startswith(LEGACY_PREAMBLE):
        return PREFERRED_PREAMBLE + content[len(LEGACY_PREAMBLE) :]
    return content


async def sync_user_profile_to_memory(*, db: AsyncSession, uid: str) -> None:
    """按数据库资料更新 USER.md 的账号资料区块，保留手工内容与工作区路径隔离。"""
    row = await UserRepository(db).get_memory_profile(uid)
    if row is None:
        return
    # 首项已是展示名：优先 display_name（真实姓名），未维护时回退登录账号
    display_name, department_name, enable_memory, station_name, job_level_name = row
    profile = None
    if enable_memory:
        # 按用户画像模板写成语义字段 + 短句：称呼、部门（组织链路）、有值时的岗位与职级
        lines = [
            PROFILE_START,
            f"- 称呼：{display_name}",
            f"- 部门：{department_name or '未分配'}",
        ]
        if station_name:
            lines.append(f"- 岗位：{station_name}")
        if job_level_name:
            lines.append(f"- 职级：{job_level_name}")
        # 角色与 UID 不属于用户画像：前者属权限信息，后者对模型没有决策价值
        lines.append(PROFILE_END)
        profile = "\n".join(lines)
    await asyncio.to_thread(_update_profile_file, uid, profile)


def _update_profile_file(uid: str, profile: str | None) -> None:
    """完整读取后原子替换，超限或不安全路径失败时保持原文件。"""
    ensure_user_workspace(uid)
    workspace = Workspace(uid)
    try:
        content = workspace.read_authorized_file(PROFILE_PATH, 1024 * 1024).decode("utf-8")
    except FileNotFoundError:
        content = ""
    content = _upgrade_legacy_preamble(content)
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
