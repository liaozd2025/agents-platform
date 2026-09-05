"""Agent run repository."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm.attributes import flag_modified

from yuxi.storage.postgres.models_business import (
    AGENT_RUN_TERMINAL_STATUSES,
    AgentRun,
    AgentRunAttempt,
    Conversation,
    Message,
    SubagentThread,
)
from yuxi.utils.datetime_utils import utc_now_naive

TERMINAL_RUN_STATUSES = set(AGENT_RUN_TERMINAL_STATUSES)
LEASED_RUN_STATUSES = {"running", "cancel_requested"}
RUN_STATUS_TO_DELIVERY_STATUS = {
    "completed": "complete",
    "failed": "failed",
    "cancelled": "cancelled",
}

TOP_LEVEL_RUN_TYPES = ("chat", "resume")


def _canonical_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


def _payload_digest(payload: dict) -> str:
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _requires_sandbox_runtime_cleanup(run: AgentRun) -> bool:
    """判断终态 Run 是否拥有官方 Sandbox runtime。"""

    runtime = run.input_payload.get("runtime") if isinstance(run.input_payload, dict) else None
    return run.run_type != "subagent" and not (isinstance(runtime, dict) and runtime.get("executor") == "pi")


class AgentRunRepository:
    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def get_run(self, run_id: str) -> AgentRun | None:
        result = await self.db.execute(select(AgentRun).where(AgentRun.id == run_id))
        return result.scalar_one_or_none()

    async def get_run_by_request_id(self, request_id: str) -> AgentRun | None:
        result = await self.db.execute(select(AgentRun).where(AgentRun.request_id == request_id))
        return result.scalar_one_or_none()

    async def get_run_for_user(self, run_id: str, uid: str) -> AgentRun | None:
        result = await self.db.execute(select(AgentRun).where(and_(AgentRun.id == run_id, AgentRun.uid == str(uid))))
        return result.scalar_one_or_none()

    async def lock_run_for_user(self, run_id: str, uid: str) -> AgentRun | None:
        """锁定用户 Run，串行化 execution tree 创建与父 Run 终态提交。"""

        result = await self.db.execute(
            select(AgentRun).where(and_(AgentRun.id == run_id, AgentRun.uid == str(uid))).with_for_update()
        )
        return result.scalar_one_or_none()

    async def get_subagent_run_with_creator(
        self,
        *,
        uid: str,
        created_by_run_id: str,
        run_id: str,
    ) -> tuple[AgentRun, AgentRun] | None:
        """读取父子 Run，并校验当前执行树的线程关系一致性。"""
        creator_run = await self.get_run_for_user(created_by_run_id, uid)
        if not creator_run:
            return None

        run = await self.get_run_for_user(run_id, uid)
        if not run or run.run_type != "subagent":
            return None
        if run.created_by_run_id != creator_run.id:
            return None

        relation_id = run.subagent_thread_relation_id
        if not relation_id:
            return None
        result = await self.db.execute(
            select(SubagentThread).where(
                SubagentThread.id == relation_id,
                SubagentThread.uid == str(uid),
            )
        )
        relation = result.scalar_one_or_none()
        if not relation or relation.parent_conversation_id != creator_run.conversation_id:
            return None
        if (
            relation.child_conversation_id != run.conversation_id
            or relation.child_thread_id != run.conversation_thread_id
        ):
            return None
        return creator_run, run

    async def get_latest_subagent_run_by_thread_for_user(
        self, conversation_thread_id: str, uid: str
    ) -> AgentRun | None:
        """读取某个子线程最近一次子智能体 run，用于状态页和继续线程校验。"""
        result = await self.db.execute(
            select(AgentRun)
            .where(
                AgentRun.conversation_thread_id == conversation_thread_id,
                AgentRun.uid == str(uid),
                AgentRun.run_type == "subagent",
            )
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_latest_run_by_thread_for_user(self, conversation_thread_id: str, uid: str) -> AgentRun | None:
        """读取线程最近一次 run，用于恢复查询 checkpoint 时的运行时模型。"""
        result = await self.db.execute(
            select(AgentRun)
            .where(
                AgentRun.conversation_thread_id == conversation_thread_id,
                AgentRun.uid == str(uid),
                AgentRun.run_type.in_(["chat", "resume", "subagent"]),
            )
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_latest_chat_or_resume_run(
        self,
        *,
        uid: str,
        agent_slug: str,
        conversation_thread_id: str,
    ) -> AgentRun | None:
        """读取队列作用域内最新的顶层 chat/resume run。"""
        result = await self.db.execute(
            select(AgentRun)
            .where(
                AgentRun.uid == str(uid),
                AgentRun.agent_slug == agent_slug,
                AgentRun.conversation_thread_id == conversation_thread_id,
                AgentRun.run_type.in_(TOP_LEVEL_RUN_TYPES),
            )
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_latest_top_level_runs_for_threads(
        self, uid: str, conversation_thread_ids: list[str]
    ) -> dict[str, tuple[str, str]]:
        """批量读取各线程最新顶层 chat/resume run，返回 thread_id -> (run_id, status)。

        使用窗口函数一次查询完成，避免对每个线程执行 N+1 查询。
        """
        if not conversation_thread_ids:
            return {}

        ranked = (
            select(
                AgentRun.id,
                AgentRun.status,
                AgentRun.conversation_thread_id,
                func.row_number()
                .over(
                    partition_by=AgentRun.conversation_thread_id,
                    order_by=(AgentRun.created_at.desc(), AgentRun.id.desc()),
                )
                .label("rn"),
            )
            .where(
                AgentRun.uid == str(uid),
                AgentRun.conversation_thread_id.in_(conversation_thread_ids),
                AgentRun.run_type.in_(TOP_LEVEL_RUN_TYPES),
            )
            .subquery()
        )
        result = await self.db.execute(select(ranked).where(ranked.c.rn == 1))
        return {row.conversation_thread_id: (row.id, row.status) for row in result.all()}

    async def list_child_runs_for_user(self, created_by_run_id: str, uid: str) -> list[AgentRun]:
        """列出由指定 run 创建的所有子 run。"""
        result = await self.db.execute(
            select(AgentRun)
            .where(
                AgentRun.created_by_run_id == created_by_run_id,
                AgentRun.uid == str(uid),
            )
            .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
        )
        return list(result.scalars().all())

    async def get_active_run_by_thread_for_user(
        self,
        *,
        agent_slug: str,
        conversation_thread_id: str,
        uid: str,
    ) -> AgentRun | None:
        """检查同一用户、智能体、线程上是否已有未结束 run，避免并发写同一线程。"""
        result = await self.db.execute(
            select(AgentRun)
            .where(
                AgentRun.agent_slug == agent_slug,
                AgentRun.uid == str(uid),
                AgentRun.conversation_thread_id == conversation_thread_id,
                or_(
                    AgentRun.status.notin_(TERMINAL_RUN_STATUSES),
                    AgentRun.runtime_cleanup_pending.is_(True),
                ),
            )
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_active_run_by_runtime_scope_for_user(
        self,
        *,
        runtime_scope_id: str,
        uid: str,
    ) -> AgentRun | None:
        """读取共享同一 runtime 的任意未终态 Run。"""
        result = await self.db.execute(
            select(AgentRun)
            .where(
                AgentRun.runtime_scope_id == str(runtime_scope_id),
                AgentRun.uid == str(uid),
                or_(
                    AgentRun.status.notin_(TERMINAL_RUN_STATUSES),
                    AgentRun.runtime_cleanup_pending.is_(True),
                ),
            )
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def create_run(
        self,
        *,
        run_id: str,
        conversation_thread_id: str,
        runtime_scope_id: str | None = None,
        agent_slug: str,
        uid: str,
        request_id: str,
        input_payload: dict,
        source: str = "chat",
        channel: str = "web",
        external_id: str | None = None,
        origin_metadata: dict | None = None,
        conversation_id: int | None = None,
        created_by_run_id: str | None = None,
        subagent_thread_relation_id: int | None = None,
        run_type: str = "chat",
        input_message_id: int | None = None,
    ) -> AgentRun:
        """登记一条 run 记录；输入正文和图片应通过 input_message_id 指向 Message。"""
        runtime_scope = str(conversation_thread_id) if runtime_scope_id is None else str(runtime_scope_id).strip()
        run = AgentRun(
            id=run_id,
            conversation_thread_id=conversation_thread_id,
            runtime_scope_id=runtime_scope,
            agent_slug=agent_slug,
            uid=str(uid),
            request_id=request_id,
            source=source,
            channel=channel,
            external_id=external_id,
            origin_metadata=origin_metadata or {},
            conversation_id=conversation_id,
            created_by_run_id=created_by_run_id,
            subagent_thread_relation_id=subagent_thread_relation_id,
            run_type=run_type,
            input_message_id=input_message_id,
            input_payload=input_payload or {},
            status="pending",
            runtime_cleanup_pending=False,
        )
        self.db.add(run)
        await self.db.flush()
        return run

    async def set_output_message(
        self,
        run_id: str,
        message_id: int,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> AgentRun | None:
        """仅允许当前 attempt 绑定属于本 Run 的 assistant 输出。"""

        if not worker_id.strip():
            raise ValueError("worker_id 不能为空")

        run = await self._lock_run(run_id)
        if not run:
            return None

        current_time = now or utc_now_naive()
        self._require_lease_owner(run, worker_id=worker_id, now=current_time, action="持久化输出消息")

        message = await self._get_matching_output_message(run, message_id)
        if message is None:
            raise ValueError("输出消息必须属于同一 conversation、Run 和 request，且角色为 assistant")

        run.output_message_id = message_id
        run.updated_at = current_time
        await self.db.flush()
        return run

    async def lock_output_persistence(
        self,
        run_id: str,
        *,
        worker_id: str,
        conversation_thread_id: str,
        request_id: str,
        now: datetime | None = None,
    ) -> AgentRun | None:
        """在任何输出写入前锁定并验证当前 attempt 的完整因果边界。"""

        if not worker_id.strip():
            raise ValueError("worker_id 不能为空")
        run = await self._lock_run(run_id)
        if run is None:
            return None

        self._require_lease_owner(run, worker_id=worker_id, now=now or utc_now_naive(), action="持久化输出消息")
        if run.conversation_thread_id != conversation_thread_id or run.request_id != request_id:
            raise ValueError("AgentRun 输出必须属于同一 thread 和 request")
        if run.conversation_id is None:
            raise ValueError("AgentRun 输出缺少 conversation 归属")
        return run

    async def lock_memory_write(
        self,
        run_id: str,
        *,
        uid: str,
        worker_id: str,
        conversation_thread_id: str,
        request_id: str,
        now: datetime | None = None,
    ) -> AgentRun | None:
        """锁定并验证允许写入用户 Memory 的当前顶层 attempt。"""
        if not worker_id.strip():
            raise ValueError("worker_id 不能为空")
        run = await self._lock_run(run_id)
        if run is None:
            return None

        self._require_lease_owner(run, worker_id=worker_id, now=now or utc_now_naive(), action="写入 Memory")
        if (
            run.uid != str(uid)
            or run.conversation_thread_id != conversation_thread_id
            or run.request_id != request_id
            or run.run_type not in TOP_LEVEL_RUN_TYPES
        ):
            raise ValueError("Memory 写入必须属于当前用户的同一顶层 Run、thread 和 request")
        return run

    async def mark_running(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_seconds: float,
        now: datetime | None = None,
        attempt_metadata: dict | None = None,
    ) -> tuple[AgentRun | None, bool]:
        """由一个 worker 原子取得或续接尚未过期的 Run ownership。"""
        if not worker_id.strip():
            raise ValueError("worker_id 不能为空")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")

        run = await self._lock_run(run_id)
        if not run:
            return None, False
        if run.status in TERMINAL_RUN_STATUSES:
            return run, False
        if run.runtime_cleanup_pending:
            return run, False

        current_time = now or utc_now_naive()
        initial_claim = run.status == "pending" or (run.status == "cancel_requested" and run.worker_id is None)
        same_live_owner = (
            run.status in LEASED_RUN_STATUSES
            and run.worker_id == worker_id
            and run.lease_expires_at is not None
            and run.lease_expires_at > current_time
        )
        if not initial_claim and not same_live_owner:
            return run, False

        if run.status == "pending":
            run.status = "running"
        run.worker_id = worker_id
        run.heartbeat_at = current_time
        run.lease_expires_at = current_time + timedelta(seconds=lease_seconds)
        run.started_at = run.started_at or current_time
        run.updated_at = current_time
        if initial_claim:
            await self._close_open_attempts(
                run.id,
                outcome="lease_expired",
                error_type="worker_lease_expired",
                error_message="执行占有人在取得新所有权前已失联。",
                now=current_time,
            )
            max_attempt_no = await self.db.scalar(
                select(func.coalesce(func.max(AgentRunAttempt.attempt_no), 0)).where(AgentRunAttempt.run_id == run.id)
            )
            fields = dict(attempt_metadata or {})
            allowed_fields = {
                "adapter",
                "route_reason",
                "route_snapshot",
                "runtime_manifest",
                "runtime_manifest_digest",
            }
            if fields.keys() - allowed_fields:
                raise ValueError("attempt_metadata 包含不支持的字段")
            if fields and set(fields) != allowed_fields:
                raise ValueError("PI attempt_metadata 必须同时冻结 adapter、route 与 Runtime Manifest")
            self.db.add(
                AgentRunAttempt(
                    run_id=run.id,
                    attempt_no=int(max_attempt_no or 0) + 1,
                    worker_id=worker_id,
                    started_at=current_time,
                    heartbeat_at=current_time,
                    lease_expires_at=run.lease_expires_at,
                    **fields,
                )
            )
        else:
            attempt = await self._get_open_attempt(run_id, worker_id=worker_id)
            if attempt is not None:
                attempt.heartbeat_at = current_time
                attempt.lease_expires_at = run.lease_expires_at
                attempt.updated_at = current_time
        await self.db.flush()
        return run, True

    async def renew_lease(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> bool:
        """仅允许当前且尚未过期的 owner 续租。"""
        if not worker_id.strip():
            raise ValueError("worker_id 不能为空")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")

        run = await self._lock_run(run_id)
        current_time = now or utc_now_naive()
        if (
            not run
            or run.status not in LEASED_RUN_STATUSES
            or run.worker_id != worker_id
            or run.lease_expires_at is None
            or run.lease_expires_at <= current_time
        ):
            return False

        run.heartbeat_at = current_time
        run.lease_expires_at = current_time + timedelta(seconds=lease_seconds)
        run.updated_at = current_time
        attempt = await self._get_open_attempt(run_id, worker_id=worker_id)
        if attempt is not None:
            attempt.heartbeat_at = current_time
            attempt.lease_expires_at = run.lease_expires_at
            attempt.updated_at = current_time
        await self.db.flush()
        return True

    async def release_lease_for_retry(
        self,
        run_id: str,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> bool:
        """仅由 lease 尚有效的当前 attempt 释放 retry ownership。"""
        run = await self._lock_run(run_id)
        current_time = now or utc_now_naive()
        if (
            not run
            or run.status != "running"
            or run.worker_id != worker_id
            or run.lease_expires_at is None
            or run.lease_expires_at <= current_time
        ):
            return False

        run.status = "pending"
        run.worker_id = None
        run.heartbeat_at = None
        run.lease_expires_at = None
        run.runtime_cleanup_pending = _requires_sandbox_runtime_cleanup(run)
        run.updated_at = current_time
        await self._finish_open_attempt(
            run_id,
            worker_id=worker_id,
            outcome="retry_released",
            now=current_time,
        )
        await self.db.flush()
        return True

    async def reconcile_expired_leases(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[list[AgentRun], list[tuple[str, str]]]:
        """把失去 owner 的活跃 Run 原子收敛为失败事实。"""
        current_time = now or utc_now_naive()
        lease_missing_or_expired = or_(
            AgentRun.lease_expires_at.is_(None),
            AgentRun.lease_expires_at <= current_time,
        )
        result = await self.db.execute(
            select(AgentRun)
            .where(
                or_(
                    and_(AgentRun.status == "running", lease_missing_or_expired),
                    and_(
                        AgentRun.status == "cancel_requested",
                        AgentRun.worker_id.is_not(None),
                        lease_missing_or_expired,
                    ),
                    and_(
                        AgentRun.status == "cancel_requested",
                        AgentRun.worker_id.is_(None),
                        AgentRun.started_at.is_not(None),
                    ),
                )
            )
            .with_for_update(skip_locked=True)
        )
        runs = list(result.scalars().all())
        creator = aliased(AgentRun)
        creator_cannot_resume = ~or_(
            creator.status == "pending",
            and_(
                creator.status.in_(LEASED_RUN_STATUSES),
                creator.worker_id.is_not(None),
                creator.lease_expires_at.is_not(None),
                creator.lease_expires_at > current_time,
            ),
        )
        pending_sandbox = and_(
            AgentRun.run_type == "sandbox",
            AgentRun.status == "pending",
            AgentRun.worker_id.is_(None),
            AgentRun.created_by_run_id.is_not(None),
        )
        orphaned_result = await self.db.execute(
            select(AgentRun)
            .join(creator, creator.id == AgentRun.created_by_run_id)
            .where(pending_sandbox, creator_cannot_resume)
            .with_for_update(of=(AgentRun, creator), skip_locked=True)
        )
        runs.extend(orphaned_result.scalars().all())
        creator_exists = select(creator.id).where(creator.id == AgentRun.created_by_run_id).exists()
        missing_creator_result = await self.db.execute(
            select(AgentRun).where(pending_sandbox, ~creator_exists).with_for_update(of=AgentRun, skip_locked=True)
        )
        runs.extend(missing_creator_result.scalars().all())
        runs.sort(key=lambda run: run.created_by_run_id is not None)
        reconciled_runs: list[AgentRun] = []
        cancelled_descendants: list[tuple[str, str]] = []
        for run in runs:
            if run.status in TERMINAL_RUN_STATUSES:
                continue
            orphaned_sandbox = run.run_type == "sandbox" and run.status == "pending"
            run.status = "failed"
            run.error_type = "sandbox_parent_unavailable" if orphaned_sandbox else "worker_lease_expired"
            run.error_message = (
                "父 Run 已无法继续接管 PI child；本次运行结果未知，需按 at-least-once 语义检查副作用。"
                if orphaned_sandbox
                else "执行 worker 的 lease 已过期；本次运行结果未知，需按 at-least-once 语义检查副作用。"
            )
            run.finished_at = current_time
            run.updated_at = current_time
            run.worker_id = None
            run.heartbeat_at = None
            run.lease_expires_at = None
            run.runtime_cleanup_pending = _requires_sandbox_runtime_cleanup(run)
            await self._project_input_delivery_status(run)
            await self._close_open_attempts(
                run.id,
                outcome="parent_unavailable" if orphaned_sandbox else "lease_expired",
                error_type=run.error_type,
                error_message=run.error_message,
                now=current_time,
            )
            reconciled_runs.append(run)
            cancelled_descendants.extend(await self.cancel_active_execution_tree_descendants(run))
        if reconciled_runs:
            await self.db.flush()
        return reconciled_runs, cancelled_descendants

    async def fail_nonterminal_for_storage_migration(self) -> list[str]:
        """停机迁移时把已失去运行环境的 Run 收敛为可观察失败事实。"""
        current_time = utc_now_naive()
        result = await self.db.execute(
            select(AgentRun)
            .where(AgentRun.status.notin_(TERMINAL_RUN_STATUSES))
            .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
            .with_for_update()
        )
        run_ids: list[str] = []
        for run in result.scalars().all():
            run.status = "failed"
            run.error_type = "storage_migration"
            run.error_message = "存储升级已停止旧运行环境；本次运行未完成"
            run.finished_at = current_time
            run.updated_at = current_time
            run.worker_id = None
            run.heartbeat_at = None
            run.lease_expires_at = None
            # quiescence proof 已证明旧 runtime 不存在，无需再创建异步清理任务。
            run.runtime_cleanup_pending = False
            await self._project_input_delivery_status(run)
            await self._close_open_attempts(
                run.id,
                outcome="failed",
                error_type=run.error_type,
                error_message=run.error_message,
                now=current_time,
            )
            run_ids.append(run.id)
        if run_ids:
            await self.db.flush()
        return run_ids

    async def request_cancel_execution_tree(
        self,
        *,
        run_id: str,
        uid: str,
        cascade_descendants: bool,
    ) -> tuple[AgentRun | None, list[str]]:
        """按 root 到 descendants 的固定锁顺序取消一棵执行树。"""
        run = await self.lock_run_for_user(run_id, str(uid))
        if run is None:
            return None, []
        await self._request_cancel_locked(run)
        cancelled_ids = [run.id]
        if cascade_descendants:
            cancelled_ids.extend(
                child_id for child_id, _thread_id in await self.cancel_active_execution_tree_descendants(run)
            )
        return run, cancelled_ids

    async def _request_cancel_locked(self, run: AgentRun) -> None:
        """转换一条已由当前事务锁定的 Run。"""
        if run.status in TERMINAL_RUN_STATUSES:
            return
        current_time = utc_now_naive()
        if run.status == "pending" and run.worker_id is None and run.started_at is None:
            run.status = "cancelled"
            run.error_type = "cancelled"
            run.error_message = "对话已在执行前取消"
            run.finished_at = current_time
            run.updated_at = current_time
            run.runtime_cleanup_pending = False
            await self._project_input_delivery_status(run)
            await self.db.flush()
            return
        run.status = "cancel_requested"
        run.updated_at = current_time
        await self.db.flush()

    async def cancel_active_execution_tree_descendants(self, root_run: AgentRun) -> list[tuple[str, str]]:
        """在父 Run 状态事务内请求仍活跃的 execution tree 后代停止。"""

        current_time = utc_now_naive()
        cancelled: list[tuple[str, str]] = []
        pending_parent_ids = [root_run.id]
        seen_ids: set[str] = set()
        while pending_parent_ids:
            parent_ids = pending_parent_ids
            pending_parent_ids = []
            result = await self.db.execute(
                select(AgentRun)
                .where(
                    AgentRun.created_by_run_id.in_(parent_ids),
                    AgentRun.uid == str(root_run.uid),
                    AgentRun.runtime_scope_id == str(root_run.runtime_scope_id),
                    AgentRun.status.notin_(TERMINAL_RUN_STATUSES),
                )
                .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
                .with_for_update()
            )
            for child in result.scalars().all():
                if child.id in seen_ids:
                    continue
                seen_ids.add(child.id)
                pending_parent_ids.append(child.id)
                child.error_type = "execution_tree_closed"
                child.error_message = "父运行已结束，请停止共享执行树"
                if child.status == "pending" and child.worker_id is None and child.started_at is None:
                    child.status = "cancelled"
                    child.finished_at = current_time
                    child.worker_id = None
                    child.heartbeat_at = None
                    child.lease_expires_at = None
                    await self._project_input_delivery_status(child)
                    await self._close_open_attempts(
                        child.id,
                        outcome="cancelled",
                        error_type=child.error_type,
                        error_message=child.error_message,
                        now=current_time,
                    )
                else:
                    child.status = "cancel_requested"
                child.updated_at = current_time
                cancelled.append((child.id, child.conversation_thread_id))

        if cancelled:
            await self.db.flush()
        return cancelled

    async def set_terminal_status(
        self,
        run_id: str,
        *,
        status: str,
        error_type: str | None = None,
        error_message: str | None = None,
        token_usage: dict | None = None,
        worker_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[AgentRun | None, bool]:
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"不支持的 AgentRun 终态：{status}")

        run = await self._lock_run(run_id)
        if not run:
            return None, False
        if run.status in TERMINAL_RUN_STATUSES:
            if run.worker_id is not None or run.heartbeat_at is not None or run.lease_expires_at is not None:
                run.worker_id = None
                run.heartbeat_at = None
                run.lease_expires_at = None
                await self.db.flush()
            return run, False

        current_time = now or utc_now_naive()
        if run.status == "pending":
            if worker_id is not None or status not in {"failed", "cancelled"}:
                return run, False
        elif run.status in LEASED_RUN_STATUSES:
            if run.worker_id != worker_id or run.lease_expires_at is None or run.lease_expires_at <= current_time:
                return run, False
            if run.status == "cancel_requested" and status != "cancelled":
                return run, False
            if run.status == "running" and status == "cancelled":
                return run, False
        else:
            return run, False

        if status == "completed":
            if run.output_message_id is None or not await self._get_matching_output_message(
                run,
                run.output_message_id,
            ):
                raise ValueError("AgentRun 完成前必须绑定同一 Run 的有效 assistant 输出消息")

        run.status = status
        run.error_type = error_type
        run.error_message = error_message
        run.token_usage = token_usage or {}
        run.finished_at = current_time
        run.updated_at = run.finished_at
        run.worker_id = None
        run.heartbeat_at = None
        run.lease_expires_at = None
        run.runtime_cleanup_pending = _requires_sandbox_runtime_cleanup(run)
        await self._project_input_delivery_status(run)
        await self._finish_open_attempt(
            run.id,
            worker_id=worker_id,
            # 调用点已校验 status 属于终态集合，attempt outcome 与 Run 终态同词表。
            outcome=status,
            error_type=error_type,
            error_message=error_message,
            now=current_time,
        )
        await self.db.flush()
        return run, True

    async def list_pending_runtime_cleanups(self, *, limit: int = 100) -> list[AgentRun]:
        """列出仍由 PostgreSQL 持久拥有的根 runtime 清理任务。"""
        result = await self.db.execute(
            select(AgentRun)
            .where(
                AgentRun.runtime_cleanup_pending.is_(True),
                AgentRun.run_type != "subagent",
            )
            .order_by(AgentRun.finished_at.asc(), AgentRun.id.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def _project_input_delivery_status(self, run: AgentRun) -> None:
        """在 owning transaction 内同步输入消息的终态投影。"""
        delivery_status = RUN_STATUS_TO_DELIVERY_STATUS.get(run.status)
        if run.input_message_id is None or delivery_status is None:
            return
        await self.db.execute(
            update(Message).where(Message.id == run.input_message_id).values(delivery_status=delivery_status)
        )

    async def record_run_manifest(
        self,
        run_id: str,
        *,
        manifest: dict,
        fingerprint: str,
        worker_id: str,
        now: datetime | None = None,
    ) -> tuple[AgentRun | None, bool]:
        """由当前 lease owner 在首次执行前 write-once 固化运行清单。

        已固化的 manifest 不可改写：重复投递幂等跳过，保证配置后续变化
        不会改写历史 Run 的事实。写入者必须是仍持有有效 lease 的 owner。
        """
        if not worker_id.strip():
            raise ValueError("worker_id 不能为空")
        if not fingerprint.strip():
            raise ValueError("fingerprint 不能为空")

        run = await self._lock_run(run_id)
        if not run:
            return None, False
        current_time = now or utc_now_naive()
        self._require_lease_owner(run, worker_id=worker_id, now=current_time, action="固化运行清单")

        if run.manifest_fingerprint is not None:
            return run, False

        run.manifest = manifest
        run.manifest_fingerprint = fingerprint
        run.manifest_recorded_at = current_time
        run.updated_at = current_time
        await self.db.flush()
        return run, True

    async def list_run_attempts(self, run_id: str) -> list[AgentRunAttempt]:
        """按执行序号读取一个 Run 的完整 attempt 历史。"""
        result = await self.db.execute(
            select(AgentRunAttempt)
            .where(AgentRunAttempt.run_id == run_id)
            .order_by(AgentRunAttempt.attempt_no.asc(), AgentRunAttempt.id.asc())
        )
        return list(result.scalars().all())

    async def get_current_run_attempt(self, run_id: str, *, worker_id: str) -> AgentRunAttempt | None:
        """读取当前 lease owner 的开放 attempt。"""

        return await self._get_open_attempt(run_id, worker_id=worker_id)

    async def get_previous_pi_session(self, *, run_id: str, uid: str, project_id: str) -> dict | None:
        """仅从同用户、Project、child 会话的已 ACK 历史选取明确 session。"""
        current = await self.db.scalar(
            select(AgentRun)
            .join(Conversation, Conversation.id == AgentRun.conversation_id)
            .where(
                AgentRun.id == run_id,
                AgentRun.uid == str(uid),
                AgentRun.run_type == "sandbox",
                Conversation.uid == str(uid),
                Conversation.project_id == project_id,
                Conversation.status == "subagent",
            )
        )
        if current is None:
            raise ValueError("PI session 当前运行的用户、Project 或 child 会话不匹配")
        row = (
            await self.db.execute(
                select(AgentRun, AgentRunAttempt, Message)
                .join(AgentRunAttempt, AgentRunAttempt.run_id == AgentRun.id)
                .join(Message, and_(Message.id == AgentRun.output_message_id, Message.run_id == AgentRun.id))
                .where(
                    AgentRun.uid == str(uid),
                    AgentRun.conversation_id == current.conversation_id,
                    AgentRun.run_type == "sandbox",
                    AgentRun.status == "completed",
                    AgentRun.id != current.id,
                    AgentRunAttempt.final_acked_at.is_not(None),
                    AgentRunAttempt.final_acked_at <= current.created_at,
                )
                .order_by(AgentRunAttempt.final_acked_at.desc(), AgentRunAttempt.id.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        prior, attempt, message = row
        pi = (message.extra_metadata or {}).get("pi") or {}
        expected = f"pi-runs/{hashlib.sha256(f'{prior.id}:{attempt.id}'.encode()).hexdigest()[:24]}"
        if pi.get("output_subdir") != expected or not isinstance(pi.get("session"), dict):
            raise ValueError("已 ACK 的 PI session 引用不完整")
        return {"run_id": prior.id, "attempt_id": attempt.id, "output_subdir": expected, "ref": pi["session"]}

    async def bind_pi_instance(
        self,
        run_id: str,
        *,
        attempt_id: int,
        instance_id: str,
        worker_id: str,
        now: datetime | None = None,
    ) -> bool:
        """由当前 owner write-once 绑定 adapter 创建的实例。"""

        if not instance_id.strip():
            raise ValueError("PI instance_id 不能为空")
        run = await self._lock_run(run_id)
        current_time = now or utc_now_naive()
        if run is None:
            raise ValueError(f"AgentRun 不存在: {run_id}")
        self._require_lease_owner(run, worker_id=worker_id, now=current_time, action="绑定 PI instance")
        attempt = await self.db.scalar(
            select(AgentRunAttempt)
            .where(AgentRunAttempt.id == attempt_id, AgentRunAttempt.run_id == run_id)
            .with_for_update()
        )
        if attempt is None or attempt.finished_at is not None or attempt.worker_id != worker_id:
            raise ValueError("只有当前 PI attempt 可以绑定 instance")
        if attempt.instance_id is not None:
            if attempt.instance_id != instance_id:
                raise ValueError("PI attempt 已绑定其他 instance")
            return False
        attempt.instance_id = instance_id
        attempt.updated_at = current_time
        await self.db.flush()
        return True

    async def record_pi_envelope(
        self,
        run_id: str,
        *,
        attempt_id: int,
        envelope: dict,
        worker_id: str,
        now: datetime | None = None,
    ) -> dict[str, bool]:
        """幂等持久化 PI envelope；final 与 Message、Run 终态在同一事务。"""

        run = await self._lock_run(run_id)
        if run is None:
            raise ValueError(f"AgentRun 不存在: {run_id}")
        attempt = await self.db.scalar(
            select(AgentRunAttempt)
            .where(AgentRunAttempt.id == attempt_id, AgentRunAttempt.run_id == run_id)
            .with_for_update()
        )
        if attempt is None:
            raise ValueError("PI attempt 不存在")
        self._validate_pi_envelope(run, attempt, envelope)

        events = list(attempt.result_events or [])
        existing = next((item for item in events if item.get("event_id") == envelope["event_id"]), None)
        if existing is not None:
            if _canonical_json(existing) != _canonical_json(envelope):
                raise ValueError("同一 PI event_id 的 envelope 内容冲突")
            return {"ack": True, "duplicate": True}

        current_time = now or utc_now_naive()
        if attempt.finished_at is not None or attempt.worker_id != worker_id:
            raise ValueError("只有当前 PI attempt 可以持久化新结果")
        self._require_lease_owner(run, worker_id=worker_id, now=current_time, action="持久化 PI 结果")
        if any(item.get("sequence") == envelope["sequence"] for item in events):
            raise ValueError("同一 PI attempt 的 sequence 不可重复")

        # ponytail: T2 事件量很小，先随 attempt 保存；出现大流量日志时再拆事件表。
        events.append(dict(envelope))
        attempt.result_events = events
        flag_modified(attempt, "result_events")
        attempt.updated_at = current_time
        if envelope["type"] != "final":
            await self.db.flush()
            return {"ack": True, "duplicate": False}

        payload = envelope["payload"]
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("PI final payload 缺少文本结果")
        output_subdir = str(payload.get("output_subdir") or "")
        expected_output_subdir = f"pi-runs/{hashlib.sha256(f'{run.id}:{attempt.id}'.encode()).hexdigest()[:24]}"
        if output_subdir != expected_output_subdir:
            raise ValueError("PI final payload 缺少有效输出目录")
        message = Message(
            conversation_id=run.conversation_id,
            role="assistant",
            content=text,
            message_type="text",
            extra_metadata={
                "pi": {
                    "artifact": payload.get("artifact"),
                    "patch": payload.get("patch"),
                    "session": payload.get("session"),
                    "output_subdir": output_subdir,
                    "runtime_manifest_digest": envelope["runtime_manifest_digest"],
                }
            },
            run_id=run.id,
            request_id=run.request_id,
            delivery_status="complete",
        )
        self.db.add(message)
        await self.db.flush()
        run.output_message_id = message.id
        attempt.final_acked_at = current_time
        _, changed = await self.set_terminal_status(
            run_id,
            status="completed",
            token_usage=self._pi_token_usage(payload.get("token_usage"), attempt.runtime_manifest),
            worker_id=worker_id,
            now=current_time,
        )
        if not changed:
            raise ValueError("PI final 未能提交当前 Run 终态")
        return {"ack": True, "duplicate": False}

    @staticmethod
    def _pi_token_usage(usage: dict | None, manifest: dict | None) -> dict:
        """校验 PI wire 用量与当前模型归属，未知不伪装为已上报零消耗。"""
        if usage is None:
            return {"available": False}
        counters = ("model_call_count", "usage_reported_call_count", "usage_unavailable_call_count")
        if not isinstance(usage, dict) or usage.get("schema_version") != 2:
            raise ValueError("PI token_usage schema 无效")
        if any(type(usage.get(key)) is not int or usage[key] < 0 for key in counters):
            raise ValueError("PI token_usage 调用计数无效")
        count, reported, unavailable = (usage[key] for key in counters)
        if reported + unavailable != count or usage.get("complete") is not (count > 0 and reported == count):
            raise ValueError("PI token_usage 上报状态不一致")
        total = usage.get("total")
        keys = ("input_tokens", "output_tokens", "total_tokens")
        if not isinstance(total, dict) or any(type(total.get(key)) is not int or total[key] < 0 for key in keys):
            raise ValueError("PI token_usage token 计数无效")
        if total["input_tokens"] + total["output_tokens"] != total["total_tokens"]:
            raise ValueError("PI token_usage 总量不一致")
        model = (manifest or {}).get("model") or {}
        spec = model.get("spec") or f"yuxi:{model.get('model_id')}"
        models = usage.get("models")
        if not isinstance(models, dict) or set(models) != {spec}:
            raise ValueError("PI token_usage 模型归属不一致")
        bucket = models[spec]
        if not isinstance(bucket, dict) or any(bucket.get(key) != usage[key] for key in counters[:2]):
            raise ValueError("PI token_usage 模型调用计数不一致")
        observed = bucket.get("usage") or {}
        if reported == 0:
            if observed or any(total.values()):
                raise ValueError("PI 未上报用量不能声明已知 token 数量")
        elif any(observed.get(key) != total[key] for key in keys) or total["total_tokens"] == 0:
            raise ValueError("PI token_usage 模型总量不一致")
        return usage

    async def record_pi_cleanup_failure(
        self,
        run_id: str,
        *,
        attempt_id: int,
        worker_id: str,
        error_message: str,
        now: datetime | None = None,
    ) -> None:
        """在 attempt 事实上记录无法确认的实例清理，供 orphan 收敛读取。"""

        attempt = await self.db.scalar(
            select(AgentRunAttempt)
            .where(AgentRunAttempt.id == attempt_id, AgentRunAttempt.run_id == run_id)
            .with_for_update()
        )
        if attempt is None or attempt.worker_id != worker_id or attempt.adapter != "local":
            raise ValueError("PI cleanup failure 与 attempt 归属不一致")
        current_time = now or utc_now_naive()
        attempt.cleanup_error = error_message
        attempt.cleanup_failed_at = current_time
        attempt.updated_at = current_time
        await self.db.flush()

    async def list_pi_cleanup_failures(self, *, limit: int = 100) -> list[dict]:
        """读取需要重试删除的 Local PI attempt。"""

        result = await self.db.execute(
            select(
                AgentRunAttempt.id.label("attempt_id"),
                AgentRunAttempt.run_id,
                AgentRunAttempt.instance_id,
                AgentRunAttempt.error_type,
                AgentRunAttempt.final_acked_at,
                AgentRunAttempt.cleanup_failed_at,
                AgentRun.uid,
                AgentRun.run_type,
                AgentRun.runtime_scope_id,
                AgentRun.input_payload,
            )
            .join(AgentRun, AgentRun.id == AgentRunAttempt.run_id)
            .where(
                AgentRunAttempt.adapter == "local",
                AgentRunAttempt.cleanup_failed_at.is_not(None),
            )
            .order_by(AgentRunAttempt.cleanup_failed_at, AgentRunAttempt.id)
            .limit(limit)
        )
        return [dict(row) for row in result.mappings().all()]

    async def clear_pi_cleanup_failure(
        self,
        attempt_id: int,
        *,
        failed_at: datetime,
        now: datetime | None = None,
    ) -> bool:
        """仅清除本次已成功收敛的 orphan 事实。"""

        result = await self.db.execute(
            update(AgentRunAttempt)
            .where(
                AgentRunAttempt.id == attempt_id,
                AgentRunAttempt.adapter == "local",
                AgentRunAttempt.cleanup_failed_at == failed_at,
            )
            .values(
                cleanup_error=None,
                cleanup_failed_at=None,
                updated_at=now or utc_now_naive(),
            )
        )
        await self.db.flush()
        return result.rowcount == 1

    async def lock_pi_cleanup_failure(self, attempt_id: int, *, failed_at: datetime) -> bool:
        """跳过其他 worker 正在收敛的同一 PI cleanup 事实。"""

        locked_id = await self.db.scalar(
            select(AgentRunAttempt.id)
            .where(
                AgentRunAttempt.id == attempt_id,
                AgentRunAttempt.adapter == "local",
                AgentRunAttempt.cleanup_failed_at == failed_at,
            )
            .with_for_update(skip_locked=True)
        )
        return locked_id is not None

    @staticmethod
    def _validate_pi_envelope(run: AgentRun, attempt: AgentRunAttempt, envelope: dict) -> None:
        """校验 envelope 的 attempt 归属及 payload/ref 摘要。"""

        required = {
            "job_id",
            "attempt_id",
            "adapter",
            "event_id",
            "sequence",
            "type",
            "runtime_manifest_digest",
        }
        if not isinstance(envelope, dict) or not required.issubset(envelope):
            raise ValueError("PI envelope 缺少必填字段")
        if (
            envelope["job_id"] != run.id
            or str(envelope["attempt_id"]) != str(attempt.id)
            or envelope["adapter"] != attempt.adapter
            or envelope["runtime_manifest_digest"] != attempt.runtime_manifest_digest
        ):
            raise ValueError("PI envelope 与 attempt 归属不一致")
        value_key = "payload" if "payload" in envelope else "ref" if "ref" in envelope else None
        if value_key is None or not isinstance(envelope[value_key], dict):
            raise ValueError("PI envelope 必须包含对象 payload 或 ref")
        digest_key = f"{value_key}_digest"
        if envelope.get(digest_key) != _payload_digest(envelope[value_key]):
            raise ValueError(f"PI envelope {digest_key} 不匹配")

    async def _get_open_attempt(self, run_id: str, *, worker_id: str | None = None) -> AgentRunAttempt | None:
        """读取该 Run 仍开放（未终结）的 attempt；指定 worker 时限定为当前 owner。"""
        conditions = [AgentRunAttempt.run_id == run_id, AgentRunAttempt.finished_at.is_(None)]
        if worker_id is not None:
            conditions.append(AgentRunAttempt.worker_id == worker_id)
        result = await self.db.execute(
            select(AgentRunAttempt).where(and_(*conditions)).order_by(AgentRunAttempt.attempt_no.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    async def _finish_open_attempt(
        self,
        run_id: str,
        *,
        worker_id: str | None,
        outcome: str,
        now: datetime,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """终结当前开放 attempt；已终结的 attempt 事实不会被改写。"""
        attempt = await self._get_open_attempt(run_id, worker_id=worker_id)
        if attempt is None:
            return
        attempt.outcome = outcome
        attempt.error_type = error_type
        attempt.error_message = error_message
        attempt.finished_at = now
        attempt.updated_at = now

    async def _close_open_attempts(
        self,
        run_id: str,
        *,
        outcome: str,
        now: datetime,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """收敛该 Run 全部仍开放的 attempt；用于失联 Run 被接管或收敛时。"""
        result = await self.db.execute(
            select(AgentRunAttempt).where(and_(AgentRunAttempt.run_id == run_id, AgentRunAttempt.finished_at.is_(None)))
        )
        for attempt in result.scalars().all():
            attempt.outcome = outcome
            attempt.error_type = error_type
            attempt.error_message = error_message
            attempt.finished_at = now
            attempt.updated_at = now

    async def _get_matching_output_message(self, run: AgentRun, message_id: int) -> Message | None:
        """读取满足 AgentRun 因果归属后置条件的输出消息。"""

        result = await self.db.execute(
            select(Message).where(
                Message.id == message_id,
                Message.conversation_id == run.conversation_id,
                Message.run_id == run.id,
                Message.request_id == run.request_id,
                Message.role == "assistant",
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _require_lease_owner(run: AgentRun, *, worker_id: str, now: datetime, action: str) -> None:
        if (
            run.status != "running"
            or run.worker_id != worker_id
            or run.lease_expires_at is None
            or run.lease_expires_at <= now
        ):
            raise ValueError(f"只有当前有效 AgentRun lease owner 可以{action}")

    async def _lock_run(self, run_id: str) -> AgentRun | None:
        result = await self.db.execute(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
        return result.scalar_one_or_none()
