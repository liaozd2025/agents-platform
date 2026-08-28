"""将 H5 的“九典 AI 助手”历史会话迁移到 Yuxi。

该脚本是一次性迁移工具，不接入在线请求链路。默认只做源库预检和数据转换，
只有显式传入 ``--apply`` 时才会写入 Yuxi PostgreSQL。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pymysql
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# 在容器中脚本位于 /app/scripts，业务包位于 /app/package；允许直接执行脚本。
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "package"))

from yuxi.storage.postgres.models_business import Agent, Conversation, Message


LOGGER = logging.getLogger("jd_ai_h5_migration")
SOURCE_NAME = "jd-ai-h5"
MIGRATION_VERSION = "v1"
SOURCE_DATABASE = "jd-ai"
SOURCE_APP_ID = 1859570229117022213
TARGET_AGENT_SLUG = "default-chatbot"


@dataclass
class MigrationStats:
    """记录预检、导入和失败数量，便于人工核对迁移结果。"""

    source_conversations: int = 0
    source_messages: int = 0
    source_files: int = 0
    prepared_conversations: int = 0
    prepared_messages: int = 0
    imported_conversations: int = 0
    imported_messages: int = 0
    skipped_conversations: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)


def _as_text(value: Any) -> str:
    """把数据库返回值转成稳定的文本，空值保持为空字符串。"""

    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip()


def _as_datetime(value: Any) -> datetime | None:
    """兼容 MySQL datetime 和 ISO 文本，并统一为 PostgreSQL 可接受的 naive 时间。"""

    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _as_text(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"无法解析时间字段: {text}") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _json_or_text(value: Any) -> Any:
    """优先还原 H5 JSON 字段；历史脏 JSON 原样保存，避免迁移时丢失信息。"""

    if value is None or isinstance(value, (dict, list, int, float, bool)):
        return value
    text = _as_text(value)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def build_yuxi_uid(user_name: str, company_code: str = "ZD") -> str:
    """按当前 OA 登录约定生成 Yuxi UID。"""

    account = _as_text(user_name)
    company = _as_text(company_code)
    if not account:
        raise ValueError("H5 用户缺少 user_name，无法建立 OA 用户映射")
    if not company:
        raise ValueError("OA company_code 不能为空")
    return f"oa:{company}:{account}"


def _is_true_flag(value: Any) -> bool:
    """把 H5 的 0/1、布尔值和文本开关统一转换为布尔值。"""

    return _as_text(value).lower() in {"1", "true", "yes", "y"}


def _source_status(value: Any) -> str:
    """把源状态限制到 Yuxi 支持的会话状态，原始值另存元数据。"""

    status = _as_text(value).lower()
    return status if status in {"active", "archived", "deleted"} else "active"


def _restore_escaped_newlines(value: Any) -> str:
    """还原 H5 文本中的字面量换行，供 Markdown 正常分段渲染。"""

    text = _as_text(value)
    # H5 历史记录将换行写成两个字符，迁移前统一恢复；不解析其他转义，避免误改内容。
    return text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")


def _message_content(message: Mapping[str, Any]) -> tuple[str, str]:
    """按照 H5 消息来源选择用户问题或助手答案。"""

    source = _as_text(message.get("from_source")).lower()
    if source == "human":
        role, content = "user", _restore_escaped_newlines(message.get("query_content"))
    elif source == "assistant":
        role, content = "assistant", _restore_escaped_newlines(message.get("answer"))
    else:
        raise ValueError(f"不支持的 H5 消息来源: {source or '<empty>'}")
    if not content:
        raise ValueError(f"H5 {source} 消息内容为空")
    return role, content


def build_conversation_payload(
    conversation: Mapping[str, Any],
    *,
    uid: str,
    batch_id: str,
    cutoff: datetime,
) -> dict[str, Any]:
    """把 H5 会话转换为 Yuxi 会话字段和来源元数据。"""

    thread_id = _as_text(conversation.get("conversation_id"))
    if not thread_id:
        raise ValueError("H5 会话缺少 conversation_id")
    if len(thread_id) > 64:
        raise ValueError(f"conversation_id 超过 Yuxi thread_id 64 字符限制: {thread_id[:20]}")

    created_at = _as_datetime(conversation.get("create_time"))
    if created_at is None:
        raise ValueError(f"会话 {thread_id} 缺少 create_time")
    updated_at = _as_datetime(conversation.get("update_time")) or created_at
    title = _as_text(conversation.get("conversation_name")) or "历史会话"
    if len(title) > 255:
        raise ValueError(f"会话 {thread_id} 标题超过 255 字符限制")

    return {
        "thread_id": thread_id,
        "uid": uid,
        "agent_id": TARGET_AGENT_SLUG,
        "title": title,
        "status": _source_status(conversation.get("status")),
        "is_pinned": _is_true_flag(conversation.get("is_top")),
        "created_at": created_at,
        "updated_at": updated_at,
        "extra_metadata": {
            "migration": {
                "source": SOURCE_NAME,
                "source_conversation_id": thread_id,
                "source_app_id": _as_text(conversation.get("app_id")) or str(SOURCE_APP_ID),
                "batch_id": batch_id,
                "version": MIGRATION_VERSION,
                "cutoff": cutoff.isoformat(sep=" "),
            },
            "source": {
                "ref_conversation_id": _as_text(conversation.get("ref_conversation_id")) or None,
                "introduction": _as_text(conversation.get("introduction")) or None,
                "source_status": _as_text(conversation.get("status")) or None,
                "source_user_id": _as_text(conversation.get("user_id")) or None,
                "source_tenant_id": _as_text(conversation.get("tenant_id")) or None,
            },
        },
    }


def build_message_payload(
    message: Mapping[str, Any],
    *,
    files: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """把一条 H5 消息转换为 Yuxi 消息，并保留来源扩展字段。"""

    role, content = _message_content(message)
    created_at = _as_datetime(message.get("create_time"))
    if created_at is None:
        raise ValueError(f"消息 {message.get('id', '<unknown>')} 缺少 create_time")

    file_metadata = [
        {
            "source_file_id": _as_text(file.get("id")) or None,
            "file_name": _as_text(file.get("file_name")) or None,
            "file_type": _as_text(file.get("file_type")) or None,
            "file_mime_type": _as_text(file.get("file_mime_type")) or None,
            "transfer_method": _as_text(file.get("transfer_method")) or None,
            "file_size": file.get("file_size"),
            "file_url": _as_text(file.get("file_url")) or None,
            "file_belongs_to": _as_text(file.get("file_belongs_to")) or None,
        }
        for file in files
    ]

    return {
        "role": role,
        "content": content,
        "message_type": "text",
        "created_at": created_at,
        "extra_metadata": {
            "migration": {
                "source": SOURCE_NAME,
                "source_message_id": _as_text(message.get("id")) or None,
                "source_conversation_id": _as_text(message.get("conversation_id")) or None,
                "version": MIGRATION_VERSION,
            },
            "source": {
                "parent_message_id": _as_text(message.get("parent_message_id")) or None,
                "trace_id": _as_text(message.get("trace_id")) or None,
                "from_source": _as_text(message.get("from_source")) or None,
                "input_params": _json_or_text(message.get("input_params")),
                "reasoning": _as_text(message.get("reasoning")) or None,
                "references": _json_or_text(message.get("references")),
                "source_status": _as_text(message.get("status")) or None,
            },
            "files": file_metadata,
        },
        "delivery_status": "complete",
    }


class H5SourceReader:
    """读取 H5 MySQL 中指定应用和截止时间之前的会话数据。"""

    def __init__(self, connection: pymysql.connections.Connection) -> None:
        self.connection = connection

    def get_server_time(self) -> datetime:
        """读取源库时间，避免宿主机和数据库时区差异影响截止时间。"""

        with self.connection.cursor() as cursor:
            cursor.execute("SELECT NOW() AS source_now")
            value = cursor.fetchone()["source_now"]
        parsed = _as_datetime(value)
        if parsed is None:
            raise RuntimeError("源数据库未返回有效 NOW()")
        return parsed

    def fetch_conversations(self, cutoff: datetime, limit: int = 0) -> list[dict[str, Any]]:
        """读取未删除且属于九典 AI 助手的会话。"""

        sql = f"""
            SELECT conversation_id, conversation_name, ref_conversation_id,
                   user_id, tenant_id, app_id, introduction, status, is_top,
                   create_time, update_time
            FROM ai_user_conversation_record
            WHERE CAST(app_id AS UNSIGNED) = %s
              AND del_flag = 0
              AND create_time <= %s
            ORDER BY create_time ASC, conversation_id ASC
        """
        if limit > 0:
            sql += " LIMIT %s"
            params: tuple[Any, ...] = (SOURCE_APP_ID, cutoff, limit)
        else:
            params = (SOURCE_APP_ID, cutoff)
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())

    def fetch_users(self, user_ids: list[Any]) -> dict[str, dict[str, Any]]:
        """读取会话涉及的 H5 用户，缺失用户由上层记录为失败。"""

        normalized_ids = [_as_text(value) for value in user_ids if _as_text(value)]
        if not normalized_ids:
            return {}
        placeholders = ", ".join(["%s"] * len(normalized_ids))
        sql = f"""
            SELECT user_id, user_name, tenant_id
            FROM sys_user
            WHERE del_flag = '0' AND user_id IN ({placeholders})
        """
        with self.connection.cursor() as cursor:
            cursor.execute(sql, normalized_ids)
            return {_as_text(row["user_id"]): row for row in cursor.fetchall()}

    def fetch_messages(self, conversation_ids: list[str], cutoff: datetime) -> list[dict[str, Any]]:
        """读取所选会话的消息，排序交由转换阶段按时间和源 ID 完成。"""

        if not conversation_ids:
            return []
        placeholders = ", ".join(["%s"] * len(conversation_ids))

        sql = f"""
            SELECT id, parent_message_id, user_id, app_id, conversation_id,
                   conversation_name, trace_id, from_source, input_params,
                   query_content, reasoning, answer, `references`, status,
                   create_time, update_time
            FROM ai_user_conversation_message_record
            WHERE CAST(app_id AS UNSIGNED) = %s
              AND del_flag = 0
              AND create_time <= %s
              AND conversation_id IN ({placeholders})
        """
        with self.connection.cursor() as cursor:
            cursor.execute(sql, (SOURCE_APP_ID, cutoff, *conversation_ids))
            return list(cursor.fetchall())

    def fetch_files(self, conversation_ids: list[str], cutoff: datetime) -> list[dict[str, Any]]:
        """只读取所选会话的附件元数据，不下载或复制 H5 文件实体。"""

        if not conversation_ids:
            return []
        placeholders = ", ".join(["%s"] * len(conversation_ids))

        # H5 两张表的字符排序规则不一致，使用数字转换避免 JOIN 触发排序规则错误。
        sql = f"""
            SELECT f.id, f.message_id, f.file_name, f.file_type,
                   f.file_mime_type, f.transfer_method, f.file_size,
                   f.file_url, f.file_belongs_to
            FROM ai_user_conversation_message_file AS f
            INNER JOIN ai_user_conversation_message_record AS m
                ON CAST(m.id AS UNSIGNED) = CAST(f.message_id AS UNSIGNED)
            WHERE CAST(m.app_id AS UNSIGNED) = %s
              AND m.del_flag = 0
              AND m.create_time <= %s
              AND m.conversation_id IN ({placeholders})
        """
        with self.connection.cursor() as cursor:
            cursor.execute(sql, (SOURCE_APP_ID, cutoff, *conversation_ids))
            return list(cursor.fetchall())


def prepare_records(
    conversations: list[Mapping[str, Any]],
    messages: list[Mapping[str, Any]],
    files: list[Mapping[str, Any]],
    users: Mapping[str, Mapping[str, Any]],
    *,
    company_code: str,
    batch_id: str,
    cutoff: datetime,
    stats: MigrationStats,
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """完成源数据校验和转换，返回可以原子写入的会话批次。"""

    messages_by_conversation: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for message in messages:
        messages_by_conversation[_as_text(message.get("conversation_id"))].append(message)

    files_by_message: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for file in files:
        files_by_message[_as_text(file.get("message_id"))].append(file)

    prepared: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for conversation in conversations:
        conversation_id = _as_text(conversation.get("conversation_id"))
        try:
            user = users.get(_as_text(conversation.get("user_id")))
            if user is None:
                raise ValueError("找不到对应的 H5 用户记录")
            uid = build_yuxi_uid(_as_text(user.get("user_name")), company_code)
            conversation_payload = build_conversation_payload(
                conversation,
                uid=uid,
                batch_id=batch_id,
                cutoff=cutoff,
            )

            message_payloads: list[dict[str, Any]] = []
            source_messages = sorted(
                messages_by_conversation.get(conversation_id, []),
                key=lambda item: (_as_datetime(item.get("create_time")) or datetime.min, _as_text(item.get("id"))),
            )
            for source_message in source_messages:
                message_id = _as_text(source_message.get("id"))
                message_payloads.append(
                    build_message_payload(source_message, files=files_by_message.get(message_id, []))
                )
            prepared.append((conversation_payload, message_payloads))
        except (TypeError, ValueError, KeyError) as exc:
            LOGGER.warning(
                "会话预检失败，已加入失败清单: conversation_id=%s reason=%s",
                conversation_id,
                exc,
            )
            stats.failures.append(
                {"conversation_id": conversation_id, "stage": "source_validation", "reason": str(exc)}
            )

    stats.prepared_conversations = len(prepared)
    stats.prepared_messages = sum(len(messages_for_conversation) for _, messages_for_conversation in prepared)
    return prepared


async def import_records(
    records: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    *,
    postgres_url: str,
    stats: MigrationStats,
) -> None:
    """逐会话事务写入 PostgreSQL，并对已迁移记录执行幂等跳过。"""

    engine = create_async_engine(postgres_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as session:
            target_agent = await session.scalar(select(Agent.slug).where(Agent.slug == TARGET_AGENT_SLUG))
            if target_agent is None:
                raise RuntimeError(f"目标 Agent 不存在或不可用: {TARGET_AGENT_SLUG}")
            LOGGER.info("目标 Agent 校验通过: %s", TARGET_AGENT_SLUG)

        for conversation_payload, message_payloads in records:
            thread_id = conversation_payload["thread_id"]
            try:
                async with session_factory() as session:
                    async with session.begin():
                        existing = await session.scalar(
                            select(Conversation).where(Conversation.thread_id == thread_id).with_for_update()
                        )
                        if existing is not None:
                            metadata = existing.extra_metadata or {}
                            migration = metadata.get("migration", {}) if isinstance(metadata, dict) else {}
                            if (
                                migration.get("source") == SOURCE_NAME
                                and migration.get("source_conversation_id") == thread_id
                            ):
                                stats.skipped_conversations += 1
                                LOGGER.info("会话已迁移，幂等跳过: thread_id=%s", thread_id)
                                continue
                            raise RuntimeError(f"目标库已有同名 thread_id，但来源不匹配: {thread_id}")

                        conversation = Conversation(**conversation_payload)
                        session.add(conversation)
                        await session.flush()
                        session.add_all(
                            [
                                Message(conversation_id=conversation.id, **message_payload)
                                for message_payload in message_payloads
                            ]
                        )
                stats.imported_conversations += 1
                stats.imported_messages += len(message_payloads)
                LOGGER.info("会话迁移成功: thread_id=%s messages=%s", thread_id, len(message_payloads))
            except Exception as exc:  # noqa: BLE001 - 单会话失败必须进入清单并继续其他会话。
                LOGGER.exception("会话写入失败，事务已回滚: thread_id=%s", thread_id)
                stats.failures.append({"conversation_id": thread_id, "stage": "target_import", "reason": str(exc)})
    finally:
        await engine.dispose()


def _parse_cutoff(value: str | None) -> datetime | None:
    """解析命令行截止时间，统一成 UTC-naive 时间。"""

    if not value:
        return None
    return _as_datetime(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="迁移 H5 九典 AI 助手历史会话到 Yuxi")
    parser.add_argument("--apply", action="store_true", help="执行 PostgreSQL 写入；默认仅预检")
    parser.add_argument("--cutoff", help="源库截止时间，例如 2026-08-25 18:00:00；不传则使用源库 NOW()")
    parser.add_argument("--limit-conversations", type=int, default=0, help="仅迁移前 N 条会话，0 表示全部")
    parser.add_argument("--batch-id", default=None, help="迁移批次标识；默认按当前 UTC 时间生成")
    parser.add_argument("--failure-file", help="将失败清单写入指定 JSON 文件")
    parser.add_argument("--company-code", default=None, help="OA UID 中的公司编码，默认读取环境变量或使用 ZD")
    return parser


def _connect_source() -> pymysql.connections.Connection:
    """从环境变量创建 H5 只读连接，密码不允许通过命令行传入。"""

    return pymysql.connect(
        host=os.getenv("H5_MYSQL_HOST", "host.docker.internal"),
        port=int(os.getenv("H5_MYSQL_PORT", "3306")),
        user=os.environ["H5_MYSQL_USER"],
        password=os.getenv("H5_MYSQL_PASSWORD", ""),
        database=os.getenv("H5_MYSQL_DATABASE", SOURCE_DATABASE),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=60,
        write_timeout=60,
        autocommit=True,
    )


def _log_summary(stats: MigrationStats, *, dry_run: bool) -> None:
    LOGGER.info(
        "迁移%s完成: source_conversations=%s source_messages=%s source_files=%s "
        "prepared_conversations=%s prepared_messages=%s imported_conversations=%s "
        "imported_messages=%s skipped_conversations=%s failures=%s",
        "预检" if dry_run else "",
        stats.source_conversations,
        stats.source_messages,
        stats.source_files,
        stats.prepared_conversations,
        stats.prepared_messages,
        stats.imported_conversations,
        stats.imported_messages,
        stats.skipped_conversations,
        len(stats.failures),
    )


async def async_main(args: argparse.Namespace) -> int:
    """执行源库读取、转换和可选的目标库导入。"""

    if args.limit_conversations < 0:
        raise ValueError("--limit-conversations 不能小于 0")
    postgres_url = os.getenv("POSTGRES_URL")
    if args.apply and not postgres_url:
        raise RuntimeError("--apply 需要配置 POSTGRES_URL")

    company_code = (
        args.company_code
        or os.getenv("OA_ACCOUNT_LOGIN_COMPANY_CODE")
        or os.getenv("OA_SSO_COMPANY_CODE")
        or "ZD"
    )
    batch_id = args.batch_id or f"{SOURCE_NAME}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    stats = MigrationStats()
    connection = _connect_source()
    try:
        reader = H5SourceReader(connection)
        cutoff = _parse_cutoff(args.cutoff) or reader.get_server_time()
        LOGGER.info(
            "开始迁移预检: source=%s app_id=%s target_agent=%s cutoff=%s apply=%s",
            SOURCE_NAME,
            SOURCE_APP_ID,
            TARGET_AGENT_SLUG,
            cutoff,
            args.apply,
        )
        conversations = reader.fetch_conversations(cutoff, args.limit_conversations)
        conversation_ids = [_as_text(row.get("conversation_id")) for row in conversations]
        messages = reader.fetch_messages(conversation_ids, cutoff)
        files = reader.fetch_files(conversation_ids, cutoff)
        users = reader.fetch_users([row.get("user_id") for row in conversations])
        stats.source_conversations = len(conversations)
        stats.source_messages = len(messages)
        stats.source_files = len(files)
        LOGGER.info(
            "源数据读取完成: conversations=%s messages=%s files=%s users=%s",
            len(conversations),
            len(messages),
            len(files),
            len(users),
        )
        records = prepare_records(
            conversations,
            messages,
            files,
            users,
            company_code=company_code,
            batch_id=batch_id,
            cutoff=cutoff,
            stats=stats,
        )
        if args.apply:
            await import_records(records, postgres_url=postgres_url, stats=stats)
        _log_summary(stats, dry_run=not args.apply)
        if args.failure_file:
            Path(args.failure_file).write_text(
                json.dumps(stats.failures, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            LOGGER.info("失败清单已写入: %s", args.failure_file)
        return 0 if not stats.failures else 2
    finally:
        connection.close()


def main() -> int:
    """命令行入口，统一输出中文日志并返回可用于脚本判断的状态码。"""

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    try:
        return asyncio.run(async_main(_build_parser().parse_args()))
    except Exception as exc:  # noqa: BLE001 - 命令行需要输出明确失败原因并退出非零。
        LOGGER.exception("迁移任务失败: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
