"""用户资料同步到 Agent 用户记忆文件的服务。"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import Department, User
from yuxi.storage.postgres.models_business import UserConfig as UserConfigRecord
from yuxi.utils.logging_config import logger
from yuxi.agents.backends.sandbox.paths import (
    ensure_thread_dirs,
    sandbox_workspace_agent_context_file,
)

PROFILE_START = "<!-- YUXI_USER_PROFILE_START -->"
PROFILE_END = "<!-- YUXI_USER_PROFILE_END -->"
PROFILE_FILENAME = "USER.md"


async def sync_user_profile_to_memory(*, db: AsyncSession, uid: str, thread_id: str) -> None:
    """按数据库最新资料更新用户工作区中的 `agents/USER.md`。"""
    normalized_uid = str(uid or "").strip()
    if not normalized_uid:
        logger.warning("用户记忆同步跳过：uid 为空")
        return

    logger.info("开始同步用户记忆：uid=%s, thread_id=%s", normalized_uid, thread_id)
    result = await db.execute(
        select(User, Department.name, UserConfigRecord.enable_memory)
        .outerjoin(Department, User.department_id == Department.id)
        .outerjoin(UserConfigRecord, UserConfigRecord.uid == User.uid)
        .where(User.uid == normalized_uid, User.is_deleted == 0)
    )
    row = result.first()
    if row is None:
        logger.warning("用户记忆同步跳过：未找到有效用户 uid=%s", normalized_uid)
        return

    user, department_name, enable_memory = row
    memory_file = sandbox_workspace_agent_context_file(thread_id, normalized_uid, PROFILE_FILENAME)
    await asyncio.to_thread(ensure_thread_dirs, thread_id, normalized_uid)
    current_content = await asyncio.to_thread(_read_text, memory_file)

    if not bool(enable_memory):
        updated_content = _remove_profile_block(current_content)
        if updated_content != current_content:
            await asyncio.to_thread(_atomic_write_text, memory_file, updated_content)
            logger.info("用户记忆同步清理完成：uid=%s（Memory 未启用）", normalized_uid)
        else:
            logger.info("用户记忆同步跳过写入：uid=%s（Memory 未启用）", normalized_uid)
        return

    profile = _render_profile(user, department_name)
    updated_content = _replace_profile_block(current_content, profile)
    if updated_content == current_content:
        logger.info("用户记忆同步无需更新：uid=%s", normalized_uid)
        return

    await asyncio.to_thread(_atomic_write_text, memory_file, updated_content)
    logger.info("用户记忆同步完成：uid=%s, department=%s", normalized_uid, department_name or "无")


def _render_profile(user: User, department_name: str | None) -> str:
    """生成仅包含允许注入 Agent 的非敏感用户资料。"""
    return "\n".join(
        (
            PROFILE_START,
            "## 用户资料",
            f"- 用户名：{user.username}",
            f"- UID：{user.uid}",
            f"- 部门：{department_name or '未分配'}",
            f"- 角色：{user.role}",
            PROFILE_END,
        )
    )


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


def _read_text(path: Path) -> str:
    """读取 UTF-8 文件；异常由调用方记录并按运行链路处理。"""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _atomic_write_text(path: Path, content: str) -> None:
    """在同目录临时文件写入后替换，避免 Agent 读到半截文件。"""
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary_path.write_text(content, encoding="utf-8")
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
