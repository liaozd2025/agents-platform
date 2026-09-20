"""停机升级时退役 PI 执行，只保留历史结果与文件。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph.message import add_messages
from sqlalchemy import text, update

from yuxi.repositories.agent_run_repository import TERMINAL_RUN_STATUSES, AgentRunRepository
from yuxi.storage.postgres.models_business import AgentRunRequest, Message
from yuxi.utils.datetime_utils import utc_now_naive


async def inspect_pi_retirement(manager) -> tuple[set[str], set[str], bool]:
    """只读取旧表与 checkpoint，确定退役范围及是否需要停机证明。"""
    async with manager.get_async_session_context() as db:
        if not await db.scalar(text("SELECT to_regclass('agent_runs')")):
            return set(), set(), False
        # 预检先于加列 DDL；读取新旧列名不能先改变仍在运行的旧数据库。
        rows = (
            (
                await db.execute(
                    text("""
            SELECT id, uid, run_type, input_payload, status,
                COALESCE(to_jsonb(agent_runs)->>'conversation_thread_id',
                         to_jsonb(agent_runs)->>'thread_id') AS conversation_thread_id,
                COALESCE(to_jsonb(agent_runs)->>'created_by_run_id',
                         to_jsonb(agent_runs)->>'parent_agent_run_id',
                         to_jsonb(agent_runs)->>'parent_run_id') AS created_by_run_id
            FROM agent_runs
            WHERE status NOT IN ('completed', 'failed', 'cancelled')
               OR run_type = 'sandbox'
               OR input_payload::jsonb #>> '{runtime,executor}' = 'pi'
        """)
                )
            )
            .mappings()
            .all()
        )
        runs = {row["id"]: row for row in rows}
        legacy_ids = {
            row["id"]
            for row in rows
            if row["run_type"] == "sandbox" or (row["input_payload"].get("runtime") or {}).get("executor") == "pi"
        }
        settled_statuses = TERMINAL_RUN_STATUSES - {"interrupted"}
        retire_ids = {run_id for run_id in legacy_ids if runs[run_id]["status"] not in settled_statuses}
        needs_quiescence = bool(legacy_ids)
        has_checkpoints = await db.scalar(text("SELECT to_regclass('checkpoint_writes')"))
        for row in rows:
            if (
                row["status"] in settled_statuses
                or row["run_type"] not in {"chat", "resume", "subagent"}
                or not has_checkpoints
            ):
                continue
            latest_id = await db.scalar(
                text("""
                SELECT id FROM agent_runs
                WHERE uid = :uid
                  AND COALESCE(to_jsonb(agent_runs)->>'conversation_thread_id',
                               to_jsonb(agent_runs)->>'thread_id') = :thread_id
                  AND run_type IN ('chat', 'resume', 'subagent')
                ORDER BY created_at DESC, id DESC LIMIT 1
            """),
                {"uid": row["uid"], "thread_id": row["conversation_thread_id"]},
            )
            if latest_id != row["id"]:
                continue
            checkpoint = await manager.get_langgraph_checkpointer().aget_tuple(
                {"configurable": {"thread_id": row["conversation_thread_id"]}}
            )
            if checkpoint and _checkpoint_has_pending_pi(checkpoint):
                retire_ids.add(row["id"])
                needs_quiescence = True

        # 父 worker 随停机失去执行位置，不能重放其尚未完成的 PI 委派。
        pending_ids = list(retire_ids)
        while pending_ids:
            parent_id = runs[pending_ids.pop()]["created_by_run_id"]
            parent = runs.get(parent_id)
            if parent and parent["status"] not in settled_statuses and parent_id not in retire_ids:
                retire_ids.add(parent_id)
                pending_ids.append(parent_id)

        request_ids = set()
        if await db.scalar(text("SELECT to_regclass('agent_run_requests')")):
            request_ids = set(
                (
                    await db.execute(
                        text("""
                SELECT request_id FROM agent_run_requests
                WHERE status = 'queued' AND (
                    input_payload::jsonb #>> '{runtime,executor}' = 'pi'
                    OR conversation_thread_id IN (
                        SELECT thread_id FROM conversations WHERE extra_metadata::jsonb ->> 'source' = 'pi_sandbox'
                    )
                )
            """)
                    )
                ).scalars()
            )
        return retire_ids, request_ids, needs_quiescence or bool(request_ids)


def _checkpoint_has_pending_pi(checkpoint) -> bool:
    """识别尚待审批或审批后尚未完成的 PI 调用，忽略已经完成的历史调用。"""
    messages = checkpoint.checkpoint.get("channel_values", {}).get("messages", [])
    for _task_id, channel, value in checkpoint.pending_writes:
        if channel == "__interrupt__":
            for interrupt in value:
                payload = getattr(interrupt, "value", None)
                if isinstance(payload, dict) and any(
                    action.get("name") == "pi_sandbox" for action in payload.get("action_requests", [])
                ):
                    return True
        elif channel == "messages":
            messages = add_messages(messages, value)
    completed_calls = set()
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            completed_calls.add(message.tool_call_id)
        elif isinstance(message, AIMessage):
            return any(
                call["name"] == "pi_sandbox" and call["id"] not in completed_calls for call in message.tool_calls
            )
    return False


async def apply_pi_retirement(db, run_ids: set[str], request_ids: set[str]) -> None:
    """在停机证明已通过后关闭旧执行意图，不删除 checkpoint、结果或 Workdir。"""
    await AgentRunRepository(db).retire_pi_runs_for_storage_migration(run_ids)
    if request_ids:
        message_ids = (
            (
                await db.execute(
                    update(AgentRunRequest)
                    .where(AgentRunRequest.request_id.in_(request_ids), AgentRunRequest.status == "queued")
                    .values(
                        status="failed", error_message="PI 执行器已停用，旧请求不会重新执行", updated_at=utc_now_naive()
                    )
                    .returning(AgentRunRequest.input_message_id)
                )
            )
            .scalars()
            .all()
        )
        if message_ids:
            await db.execute(update(Message).where(Message.id.in_(message_ids)).values(delivery_status="failed"))
