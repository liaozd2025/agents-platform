"""Dashboard 统计与监控业务用例服务。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.modules.agents.models.definitions import Agent
from yuxi.modules.agents.models.sessions import Session
from yuxi.modules.agents.repositories.messages import MessageRepository
from yuxi.modules.agents.repositories.sessions import SessionRepository
from yuxi.modules.agents.services.message_audits import serialize_tool_audit
from yuxi.modules.extensions.skills.models import Skill
from yuxi.modules.identity.permissions import (
    ResourcePermission,
    resolve_agent_permission,
    resolve_knowledge_base_permission,
    resolve_skill_permission,
)
from yuxi.modules.identity.permissions.authorization import AuthorizationContext, AuthorizationTarget, parse_department_ancestor_ids
from yuxi.modules.system.repositories.dashboard import DashboardRepository
from yuxi.modules.system.services.dashboard_scope import dashboard_resource_subjects
from yuxi.shared.datetime import format_utc_datetime


class DashboardService:
    """封装 Dashboard 统计读模型、会话查询与时间序列分析。"""

    def __init__(self, db: AsyncSession, *, session_filter=None, message_filter=None, run_filter=None, total_users=None):
        self.db = db
        self.repo = DashboardRepository(
            db, session_filter=session_filter, message_filter=message_filter, run_filter=run_filter, total_users=total_users
        )
        self.session_repo = SessionRepository(db)

    async def get_basic_stats(self) -> dict[str, Any]:
        """读取基础统计指标（会话数、消息数、用户数）。"""
        return await self.repo.get_basic_stats()

    async def get_user_activity_stats(self, *, now: datetime | None = None) -> dict[str, Any]:
        """统计用户总量与活跃趋势。"""
        return await self.repo.get_user_activity_stats(now=now)

    async def get_tool_call_stats(self, *, now: datetime | None = None) -> dict[str, Any]:
        """统计工具调用总量、成功率与分布。"""
        return await self.repo.get_tool_call_stats(now=now)

    async def get_agent_analytics(self) -> dict[str, Any]:
        """汇总智能体对话与工具使用情况。"""
        return await self.repo.get_agent_analytics()

    async def get_call_timeseries(self, *, metric_type: str, time_range: str = "14days") -> dict[str, Any]:
        """查询调用分析时间序列。"""
        return await self.repo.get_call_timeseries(
            metric_type=metric_type,
            time_range=time_range,
        )

    async def get_agent_session_distribution(self, *, time_range: str = "all") -> dict[str, Any]:
        """读取独立范围的智能体会话分布。"""
        return await self.repo.get_agent_session_distribution(time_range=time_range)

    async def get_thread_analytics(
        self,
        *,
        time_range: str = "30days",
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """汇总会话（Thread）多维分析统计。"""
        return await self.repo.get_thread_analytics(
            time_range=time_range,
            agent_id=agent_id,
        )

    async def get_session_filter_options(self) -> dict[str, list[dict[str, Any]]]:
        """读取会话审计用户与 Agent 筛选项。"""
        return await self.repo.get_session_filter_options()

    async def list_sessions(
        self,
        *,
        uid: str | None = None,
        agent_id: str | None = None,
        status: str = "all",
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页查询并组装 Dashboard 对话列表。"""
        return await self.repo.list_sessions(
            uid=uid,
            agent_id=agent_id,
            status=status,
            search=search,
            limit=limit,
            offset=offset,
        )

    async def get_session_detail(self, thread_id: str) -> dict[str, Any] | None:
        """获取指定会话完整消息流水与统计。"""
        agent_session = await self.db.scalar(select(Session).where(Session.thread_id == thread_id, self.repo.session_filter))
        if not agent_session:
            return None

        messages = await MessageRepository(self.db).get_messages(agent_session.id, scope_filter=self.repo.message_filter)
        executions = await MessageRepository(self.db).list_tool_executions(
            [message.id for message in messages], scope_filter=self.repo.message_filter
        )
        tools_by_source = {}
        for execution in executions:
            source_id = execution.extra_metadata["source_model_message_id"]
            tools_by_source.setdefault(source_id, []).append(serialize_tool_audit(execution))
        audit_metadata = await self.repo.get_session_audit_metadata(agent_session)
        message_list = []
        for message in messages:
            message_data = {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "message_type": message.message_type,
                "created_at": format_utc_datetime(message.created_at) or "",
            }
            message_data["tool_executions"] = tools_by_source.get(message.id, [])
            message_list.append(message_data)

        return {
            "thread_id": agent_session.thread_id,
            "uid": agent_session.uid,
            "username": audit_metadata["username"],
            "display_name": audit_metadata["display_name"],
            "run_status": audit_metadata["run_status"],
            "user_avatar": audit_metadata["user_avatar"],
            "user_deleted": audit_metadata["user_deleted"],
            "agent_id": agent_session.agent_id,
            "agent_name": audit_metadata["agent_name"],
            "agent_avatar": audit_metadata["agent_avatar"],
            "agent_deleted": audit_metadata["agent_deleted"],
            "title": agent_session.title,
            "status": agent_session.status,
            "is_pinned": bool(agent_session.is_pinned),
            "message_count": len(message_list),
            "created_at": format_utc_datetime(agent_session.created_at) or "",
            "updated_at": format_utc_datetime(agent_session.updated_at) or "",
            **await self.repo.get_session_token_usage(agent_session.id),
            "messages": message_list,
        }

    async def get_resource_scope_stats(
        self,
        *,
        authorization: AuthorizationContext,
        department_id: int | None,
    ) -> dict[str, Any]:
        """按创建归属和共享 ACL 汇总当前管理域资源。"""

        subjects = await dashboard_resource_subjects(self.db, authorization, department_id)

        async def summarize(model: Any, resolver: Any) -> dict[str, Any]:
            creation_count = 0
            shared_visible_count = 0
            contains_inferred_data = False
            # ponytail: 直接复用现有 ACL；资源或组织主体量出现慢查询后再下推为 SQL。
            for resource in await self.repo.list_resources(model):
                ancestor_ids = parse_department_ancestor_ids(resource.organization_path_snapshot)
                target = AuthorizationTarget(
                    owner_user_id=authorization.user.id if resource.created_by == authorization.user.uid else None,
                    department_ancestor_ids=ancestor_ids,
                )
                created_visible = (department_id is None or department_id in ancestor_ids) and authorization.allows(
                    "dashboard:view", target
                )
                shared_visible = any(resolver(subject, resource) != ResourcePermission.NONE for subject in subjects)
                creation_count += int(created_visible)
                shared_visible_count += int(shared_visible)
                contains_inferred_data = contains_inferred_data or bool(
                    (created_visible or shared_visible) and resource.organization_snapshot_inferred
                )

            return {
                "creation_count": creation_count,
                "shared_visible_count": shared_visible_count,
                "contains_inferred_data": contains_inferred_data,
            }

        from yuxi.modules.knowledge.models import KnowledgeBase

        knowledge_bases = await summarize(KnowledgeBase, resolve_knowledge_base_permission)
        agents = await summarize(Agent, resolve_agent_permission)
        skills = await summarize(Skill, resolve_skill_permission)
        return {
            "knowledge_bases": knowledge_bases,
            "agents": agents,
            "skills": skills,
            "contains_inferred_data": any(metric["contains_inferred_data"] for metric in (knowledge_bases, agents, skills)),
        }
