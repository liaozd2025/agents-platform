"""PI 沙箱 child Run 的创建边界。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

import yuxi.services.agent_run_service as agent_run_service
from yuxi.agents.backends.paths import VIRTUAL_PERSONAL_SKILLS_PATH, VIRTUAL_SKILLS_PATH
from yuxi.agents.skills.service import compute_skill_dir_hash, is_valid_skill_slug, normalize_string_list
from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.repositories.project_repository import ProjectRepository
from yuxi.services.input_message_service import build_chat_input_message
from yuxi.storage.postgres.models_business import AgentRun
from yuxi.utils.hash_utils import hash_id
from yuxi.utils.logging_config import logger

PI_SANDBOX_NAME = "PI Agent 沙箱"


@dataclass(frozen=True)
class PiSandboxStartResult:
    run: AgentRun
    created: bool


async def _compute_skill_digests(sources: dict[str, Path]) -> dict[str, str]:
    """在线程中计算 Skill 摘要，避免大目录扫描阻塞 worker 心跳。"""

    started_at = time.perf_counter()
    items = await asyncio.gather(
        *(asyncio.to_thread(compute_skill_dir_hash, source) for source in sources.values())
    )
    digests = dict(zip(sources, items, strict=True))
    logger.info(
        "PI 子 Run Skill 摘要计算完成: skills=%d elapsed=%.2fs",
        len(digests),
        time.perf_counter() - started_at,
    )
    return digests


class PiSandboxRunService:
    """把一次 ``pi_sandbox`` 调用登记为持久化 child Run。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.run_repo = AgentRunRepository(db)
        self.conv_repo = ConversationRepository(db)
        self.project_repo = ProjectRepository(db)

    async def start(
        self,
        *,
        uid: str,
        created_by_run_id: str,
        description: str,
        tool_call_id: str,
        skill_slugs: list[str],
        skill_sources: dict[str, str | Path],
        skill_runtime_paths: dict[str, str],
    ) -> PiSandboxStartResult:
        """固化 Project、模型和 Skill 快照并幂等创建 PI child Run。"""

        if not description.strip() or not tool_call_id.strip():
            raise ValueError("PI 沙箱任务和 tool_call_id 不能为空")
        slugs = [slug for slug in normalize_string_list(skill_slugs) if is_valid_skill_slug(slug)]
        sources = {slug: Path(skill_sources[slug]) for slug in slugs if slug in skill_sources}
        runtime_paths = {slug: str(skill_runtime_paths.get(slug) or "") for slug in slugs}
        if set(sources) != set(slugs) or set(skill_runtime_paths) != set(slugs):
            raise ValueError("PI 沙箱缺少已选 Skill 来源")
        for slug, runtime_path in runtime_paths.items():
            if runtime_path not in {
                f"{VIRTUAL_SKILLS_PATH}/{slug}",
                f"{VIRTUAL_PERSONAL_SKILLS_PATH}/{slug}",
            }:
                raise ValueError(f"PI 沙箱 Skill runtime 路径无效: {slug}")

        creator_run = await self.run_repo.lock_run_for_user(created_by_run_id, uid)
        if not creator_run:
            raise ValueError("父运行任务不存在")
        if creator_run.status != "running":
            raise ValueError("父运行已不再执行，不能启动 PI 沙箱")
        if creator_run.run_type == "sandbox":
            raise ValueError("PI 沙箱不能递归创建 PI 沙箱")

        parent_conversation = await self.conv_repo.get_conversation_by_id(creator_run.conversation_id)
        if parent_conversation is None or parent_conversation.uid != str(uid):
            raise ValueError("父运行任务的 Conversation 不存在")
        project = await self.project_repo.lock_active_for_user(
            parent_conversation.project_id,
            str(uid),
        )
        if project is None:
            raise ValueError("父运行任务的 Project 不存在")
        parent_conversation = await self.conv_repo.lock_conversation_by_thread_id(creator_run.conversation_thread_id)
        if (
            parent_conversation is None
            or parent_conversation.id != creator_run.conversation_id
            or parent_conversation.uid != str(uid)
            or parent_conversation.status == "deleted"
            or parent_conversation.project_id != project.id
        ):
            raise ValueError("父运行任务的 Conversation 不存在")
        workdir_path = project.workdir_path
        runtime_scope_id = str(creator_run.runtime_scope_id or creator_run.conversation_thread_id)
        child_thread_id = hash_id(
            "pi_",
            f"{uid}:{runtime_scope_id}:{creator_run.agent_slug}",
            length=64,
        )
        await self._ensure_child_conversation(
            child_thread_id=child_thread_id,
            uid=uid,
            creator_run=creator_run,
            project_id=project.id,
        )

        request_id = hash_id("req:", f"{creator_run.id}:pi-sandbox:{tool_call_id}")
        agent_kind = "subagent" if creator_run.run_type == "subagent" else "main"
        scope = await agent_run_service.prepare_agent_run_creation_scope(
            agent_slug=creator_run.agent_slug,
            conversation_thread_id=child_thread_id,
            current_uid=uid,
            db=self.db,
            request_id=request_id,
            run_type="sandbox",
            agent_kind=agent_kind,
            created_by_run_id=creator_run.id,
        )
        if scope.existing_run:
            return PiSandboxStartResult(run=scope.existing_run, created=False)

        parent_payload = creator_run.input_payload if isinstance(creator_run.input_payload, dict) else {}
        model_spec = str(parent_payload.get("model_spec") or "").strip()
        if not model_spec:
            raise ValueError("父运行任务缺少模型快照")
        # Skill 目录可能包含数千个文件；摘要计算必须离开事件循环，否则会阻塞父 Run 的 lease 心跳。
        skill_digests = await _compute_skill_digests(sources)
        input_message = build_chat_input_message(description).with_metadata(
            {"request_id": request_id, "source": "pi_sandbox"}
        )
        persisted_input = await agent_run_service.create_agent_run_input_message(
            db=self.db,
            conversation_id=scope.conversation.id,
            request_id=request_id,
            input_message=input_message,
        )
        run, created = await agent_run_service.persist_agent_run_record(
            agent_slug=creator_run.agent_slug,
            conversation_thread_id=child_thread_id,
            runtime_scope_id=runtime_scope_id,
            current_uid=uid,
            db=self.db,
            request_id=request_id,
            conversation_id=scope.conversation.id,
            run_type="sandbox",
            input_payload={
                "model_spec": model_spec,
                "tool_approval_mode": "always_trust",
                "runtime": {
                    "executor": "pi",
                    "tool_call_id": tool_call_id,
                    "parent_thread_id": creator_run.conversation_thread_id,
                    "workdir_path": workdir_path,
                    "skill_slugs": slugs,
                    "skill_digests": skill_digests,
                    "skill_runtime_paths": runtime_paths,
                },
            },
            persisted_input_message=persisted_input,
            created_by_run_id=creator_run.id,
            source="pi_sandbox",
            channel="internal",
        )
        if created:
            await self.db.commit()
        return PiSandboxStartResult(run=run, created=created)

    async def _ensure_child_conversation(
        self,
        *,
        child_thread_id: str,
        uid: str,
        creator_run: AgentRun,
        project_id: str,
    ) -> None:
        conversation = await self.conv_repo.get_conversation_by_thread_id(child_thread_id)
        if conversation:
            if (
                conversation.uid != str(uid)
                or conversation.status != "subagent"
                or conversation.agent_id != creator_run.agent_slug
                or conversation.project_id != project_id
            ):
                raise ValueError("PI 沙箱子线程已被其它对话占用")
            return
        conversation = await self.conv_repo.add_conversation(
            uid=uid,
            agent_id=creator_run.agent_slug,
            title=PI_SANDBOX_NAME,
            thread_id=child_thread_id,
            metadata={
                "source": "pi_sandbox",
                "parent_thread_id": creator_run.conversation_thread_id,
                "parent_conversation_id": creator_run.conversation_id,
            },
            project_id=project_id,
        )
        conversation.status = "subagent"
        await self.db.flush()


async def execute_pi_sandbox_run(run_id: str) -> None:
    """在父 worker 槽位内执行 child Run，避免同队列父子互等。"""

    from arq.worker import RetryJob
    from yuxi.services.run_worker import process_agent_run

    for job_try in (1, 2):
        try:
            await process_agent_run(
                {"worker_id": f"pi-sandbox-inline:{run_id}", "job_try": job_try},
                run_id,
            )
            return
        except RetryJob:
            if job_try == 2:
                raise
