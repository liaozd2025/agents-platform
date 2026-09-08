"""Dashboard 统计读模型的数据访问层。"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Integer, String, case, cast, distinct, func, literal, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.repositories.agent_repository import AgentRepository
from yuxi.storage.minio.client import normalize_public_minio_url
from yuxi.storage.postgres.models_business import (
    Agent,
    AgentRun,
    Conversation,
    ConversationStats,
    Message,
    MessageFeedback,
    OperationLog,
    SecurityAudit,
    ToolCall,
    User,
)
from yuxi.utils.datetime_utils import UTC, ensure_shanghai, shanghai_now, utc_now


def _latest_run_status_query() -> Any:
    """返回当前 Conversation 对应最新 Run 状态的相关子查询。"""
    return (
        select(AgentRun.status)
        .where(AgentRun.conversation_thread_id == Conversation.thread_id)
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(1)
        .correlate(Conversation)
        .scalar_subquery()
    )


def _conversation_user_join() -> Any:
    """兼容稳定 UID 与历史账号两种会话归属键，确保旧会话也能关联用户资料。"""

    # OA 用户迁移后 users.uid 为 oa:公司:账号，但历史 Conversation.uid 仍保存原始账号。
    return or_(Conversation.uid == User.uid, Conversation.uid == User.username)


class DashboardRepository:
    """集中封装 Dashboard 的跨表统计查询与读模型聚合。"""

    def __init__(self, db_session: AsyncSession):
        self.db_session = db_session

    @staticmethod
    def _time_group_format(column: Any, time_range: str) -> Any:
        """生成使用上海时区显示的 PostgreSQL 时间分组表达式。"""
        if time_range == "14hours":
            return func.to_char(column + text("INTERVAL '8 hours'"), "YYYY-MM-DD HH24:00")
        if time_range == "14weeks":
            return func.to_char(column + text("INTERVAL '8 hours'"), "YYYY-IW")
        return func.to_char(column + text("INTERVAL '8 hours'"), "YYYY-MM-DD")

    def _shanghai_date_group(self, column: Any) -> Any:
        """按上海日历日生成 PostgreSQL/SQLite 兼容分组表达式。"""
        bind = self.db_session.bind
        if bind is not None and bind.dialect.name == "sqlite":
            return func.date(column, "+8 hours")
        return func.date(column + text("INTERVAL '8 hours'"))

    async def list_conversations(
        self,
        *,
        uid: str | None = None,
        agent_id: str | None = None,
        status: str = "all",
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
        scope_filter: Any | None = None,
    ) -> dict[str, Any]:
        """在历史组织范围内分页查询对话，并装配当前审计信息。"""
        filters = []
        if scope_filter is not None:
            filters.append(scope_filter)
        if uid:
            filters.append(Conversation.uid == uid)
        if agent_id:
            filters.append(Conversation.agent_id == agent_id)
        if status and status != "all":
            filters.append(Conversation.status == status)
        else:
            filters.append(Conversation.status != "deleted")
        if search:
            search_term = f"%{search.strip()}%"
            filters.append(
                or_(
                    Conversation.title.ilike(search_term),
                    Conversation.thread_id.ilike(search_term),
                    Conversation.uid.ilike(search_term),
                    User.username.ilike(search_term),
                    User.display_name.ilike(search_term),
                )
            )

        total_result = await self.db_session.execute(
            select(func.count(Conversation.id))
            .select_from(Conversation)
            .outerjoin(User, _conversation_user_join())
            .where(*filters)
        )
        latest_run_status = _latest_run_status_query().label("run_status")
        rows = (
            await self.db_session.execute(
                select(Conversation, ConversationStats, User, latest_run_status)
                .outerjoin(ConversationStats, Conversation.id == ConversationStats.conversation_id)
                .outerjoin(User, _conversation_user_join())
                .where(*filters)
                .order_by(Conversation.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()

        agent_slugs = {conversation.agent_id for conversation, _, _, _ in rows if conversation.agent_id}
        agents_by_slug: dict[str, Agent] = {}
        if agent_slugs:
            agents = await AgentRepository(self.db_session).list_by_slugs(list(agent_slugs))
            agents_by_slug = {agent.slug: agent for agent in agents}

        items = []
        for conversation, stats, user, run_status in rows:
            agent = agents_by_slug.get(conversation.agent_id)
            items.append(
                {
                    "thread_id": conversation.thread_id,
                    "uid": conversation.uid,
                    "username": user.username if user else conversation.uid,
                    "display_name": user.display_name if user else None,
                    "user_avatar": normalize_public_minio_url(user.avatar) if user and user.avatar else None,
                    "user_deleted": user is None or bool(user.is_deleted),
                    "agent_id": conversation.agent_id,
                    "agent_name": agent.name if agent else conversation.agent_id,
                    "agent_avatar": normalize_public_minio_url(agent.icon) if agent and agent.icon else None,
                    "agent_deleted": agent is None,
                    "title": conversation.title,
                    "status": conversation.status,
                    "run_status": run_status,
                    "is_pinned": bool(conversation.is_pinned),
                    "message_count": stats.message_count if stats else 0,
                    "total_tokens": stats.total_tokens if stats else 0,
                    "created_at": conversation.created_at.isoformat() if conversation.created_at else "",
                    "updated_at": conversation.updated_at.isoformat() if conversation.updated_at else "",
                }
            )
        return {
            "items": items,
            "total": int(total_result.scalar() or 0),
            "limit": limit,
            "offset": offset,
        }

    async def get_conversation_filter_options(
        self,
        *,
        scope_filter: Any | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """读取当前历史组织范围内的用户与 Agent 筛选项。"""
        user_query = (
            select(Conversation.uid, User.username, User.display_name, User.avatar, User.is_deleted)
            .select_from(Conversation)
            .outerjoin(User, _conversation_user_join())
        )
        agent_query = (
            select(Conversation.agent_id, Agent.name, Agent.icon)
            .select_from(Conversation)
            .outerjoin(Agent, Conversation.agent_id == Agent.slug)
        )
        if scope_filter is not None:
            user_query = user_query.where(scope_filter)
            agent_query = agent_query.where(scope_filter)
        user_rows = (await self.db_session.execute(user_query.distinct())).all()
        agent_rows = (await self.db_session.execute(agent_query.distinct())).all()

        users = [
            {
                "uid": row.uid,
                "username": row.username or row.uid,
                "display_name": row.display_name,
                "avatar": normalize_public_minio_url(row.avatar) if row.avatar else None,
                "is_deleted": row.username is None or bool(row.is_deleted),
            }
            for row in user_rows
        ]
        agents = [
            {
                "agent_id": row.agent_id,
                "agent_name": row.name or row.agent_id,
                "avatar": normalize_public_minio_url(row.icon) if row.icon else None,
                "is_deleted": row.name is None,
            }
            for row in agent_rows
        ]
        users.sort(key=lambda item: (item["is_deleted"], item["username"].lower()))
        agents.sort(key=lambda item: (item["is_deleted"], item["agent_name"].lower()))
        return {"users": users, "agents": agents}

    async def get_conversation_audit_metadata(self, conversation: Conversation) -> dict[str, Any]:
        """读取会话关联用户与 Agent 的当前审计状态。"""
        row = (
            await self.db_session.execute(
                select(User, Agent, _latest_run_status_query().label("run_status"))
                .select_from(Conversation)
                .outerjoin(User, _conversation_user_join())
                .outerjoin(Agent, Conversation.agent_id == Agent.slug)
                .where(Conversation.id == conversation.id)
            )
        ).one()
        user, agent, run_status = row
        return {
            "username": user.username if user else conversation.uid,
            "display_name": user.display_name if user else None,
            "user_avatar": normalize_public_minio_url(user.avatar) if user and user.avatar else None,
            "user_deleted": user is None or bool(user.is_deleted),
            "agent_name": agent.name if agent else conversation.agent_id,
            "agent_avatar": normalize_public_minio_url(agent.icon) if agent and agent.icon else None,
            "agent_deleted": agent is None,
            "run_status": run_status,
        }

    async def get_user_activity_stats(
        self,
        *,
        scope_filter: Any | None = None,
        total_users: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """统计用户总量与近期开启对话的活跃用户。"""
        query_now = (now or utc_now()).replace(tzinfo=None)

        filters = (scope_filter,) if scope_filter is not None else ()
        valid_conversation = Conversation.status.notin_(("deleted", "subagent"))
        if total_users is None:
            total_result = await self.db_session.execute(select(func.count(User.id)).where(User.is_deleted == 0))
            total_users = total_result.scalar() or 0
        active_24h_result = await self.db_session.execute(
            select(func.count(distinct(User.id)))
            .select_from(Conversation)
            .join(User, _conversation_user_join())
            .where(
                Conversation.updated_at >= query_now - timedelta(days=1),
                valid_conversation,
                User.is_deleted == 0,
                *filters,
            )
        )
        active_30d_result = await self.db_session.execute(
            select(func.count(distinct(User.id)))
            .select_from(Conversation)
            .join(User, _conversation_user_join())
            .where(
                Conversation.updated_at >= query_now - timedelta(days=30),
                valid_conversation,
                User.is_deleted == 0,
                *filters,
            )
        )

        active_date = self._shanghai_date_group(Conversation.updated_at)
        daily_active_result = await self.db_session.execute(
            select(active_date.label("date"), func.count(distinct(User.id)).label("active_users"))
            .select_from(Conversation)
            .join(User, _conversation_user_join())
            .where(
                Conversation.updated_at >= query_now - timedelta(days=120),
                Conversation.updated_at < query_now,
                valid_conversation,
                User.is_deleted == 0,
                *filters,
            )
            .group_by(active_date)
        )
        daily_active_by_date = {str(row.date): int(row.active_users or 0) for row in daily_active_result.all()}
        local_today = (query_now + timedelta(hours=8)).date()
        daily_active_users = []
        for day_offset in range(119, -1, -1):
            date = (local_today - timedelta(days=day_offset)).strftime("%Y-%m-%d")
            daily_active_users.append({"date": date, "active_users": daily_active_by_date.get(date, 0)})

        return {
            "total_users": total_users,
            "active_users_24h": active_24h_result.scalar() or 0,
            "active_users_30d": active_30d_result.scalar() or 0,
            "daily_active_users": daily_active_users,
        }

    async def get_tool_call_stats(
        self,
        *,
        scope_filter: Any | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """统计工具调用总量、成功率、排行与近七日趋势。"""
        query_now = (now or utc_now()).replace(tzinfo=None)
        filters = [Conversation.status.notin_(("deleted", "subagent"))]
        if scope_filter is not None:
            filters.append(scope_filter)
        total_result = await self.db_session.execute(
            select(func.count(ToolCall.id))
            .join(Message, ToolCall.message_id == Message.id)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(*filters)
        )
        successful_result = await self.db_session.execute(
            select(func.count(ToolCall.id))
            .join(Message, ToolCall.message_id == Message.id)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(ToolCall.status == "success", *filters)
        )
        total_calls = total_result.scalar() or 0
        successful_calls = successful_result.scalar() or 0

        most_used_result = await self.db_session.execute(
            select(ToolCall.tool_name, func.count(ToolCall.id).label("count"))
            .join(Message, ToolCall.message_id == Message.id)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(*filters)
            .group_by(ToolCall.tool_name)
            .order_by(func.count(ToolCall.id).desc())
            .limit(10)
        )
        error_result = await self.db_session.execute(
            select(ToolCall.tool_name, func.count(ToolCall.id).label("error_count"))
            .join(Message, ToolCall.message_id == Message.id)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(ToolCall.status == "error", *filters)
            .group_by(ToolCall.tool_name)
        )

        daily_tool_calls = []
        for day_offset in range(7):
            day_start = query_now - timedelta(days=day_offset + 1)
            day_end = query_now - timedelta(days=day_offset)
            daily_result = await self.db_session.execute(
                select(func.count(ToolCall.id))
                .join(Message, ToolCall.message_id == Message.id)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(
                    ToolCall.created_at >= day_start,
                    ToolCall.created_at < day_end,
                    *filters,
                )
            )
            daily_tool_calls.append({"date": day_start.strftime("%Y-%m-%d"), "call_count": daily_result.scalar() or 0})

        return {
            "total_calls": total_calls,
            "successful_calls": successful_calls,
            "failed_calls": total_calls - successful_calls,
            "success_rate": round(successful_calls / total_calls * 100, 2) if total_calls else 0,
            "most_used_tools": [{"tool_name": name, "count": count} for name, count in most_used_result.all()],
            "tool_error_distribution": {name: count for name, count in error_result.all()},
            "daily_tool_calls": list(reversed(daily_tool_calls)),
        }

    async def get_agent_analytics(
        self,
        *,
        conversation_filter: Any | None = None,
        feedback_filter: Any | None = None,
        tool_filter: Any | None = None,
    ) -> dict[str, Any]:
        """汇总各智能体的对话、满意度、工具使用与名称。"""
        valid_conversation = Conversation.status.notin_(("deleted", "subagent"))
        conversation_filters = [valid_conversation]
        feedback_filters = [valid_conversation]
        tool_filters = [valid_conversation]
        if conversation_filter is not None:
            conversation_filters.append(conversation_filter)
        if feedback_filter is not None:
            feedback_filters.append(feedback_filter)
        if tool_filter is not None:
            tool_filters.append(tool_filter)

        conversation_rows = (
            await self.db_session.execute(
                select(Conversation.agent_id, func.count(Conversation.id))
                .where(*conversation_filters)
                .group_by(Conversation.agent_id)
            )
        ).all()
        conversation_counts = {agent_id: int(count or 0) for agent_id, count in conversation_rows}

        feedback_rows = (
            await self.db_session.execute(
                select(
                    Conversation.agent_id,
                    func.count(MessageFeedback.id).label("total"),
                    func.sum(case((MessageFeedback.rating == "like", 1), else_=0)).label("positive"),
                )
                .join(Message, MessageFeedback.message_id == Message.id)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(*feedback_filters)
                .group_by(Conversation.agent_id)
            )
        ).all()
        feedback_by_agent = {row.agent_id: (int(row.total or 0), int(row.positive or 0)) for row in feedback_rows}

        tool_rows = (
            await self.db_session.execute(
                select(Conversation.agent_id, func.count(ToolCall.id))
                .join(Message, ToolCall.message_id == Message.id)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(*tool_filters)
                .group_by(Conversation.agent_id)
            )
        ).all()
        tool_counts = {agent_id: int(count or 0) for agent_id, count in tool_rows}

        conversation_stats = []
        satisfaction_stats = []
        tool_usage = []
        top_agents = []
        agent_slugs = [agent_id for agent_id, _count in conversation_rows if agent_id]
        agents_by_slug = {
            agent.slug: agent for agent in await AgentRepository(self.db_session).list_by_slugs(agent_slugs)
        }
        for agent_id in agent_slugs:
            agent = agents_by_slug.get(agent_id)
            conversation_count = conversation_counts.get(agent_id, 0)
            total_feedbacks, positive_feedbacks = feedback_by_agent.get(agent_id, (0, 0))
            satisfaction_rate = round(positive_feedbacks / total_feedbacks * 100, 2) if total_feedbacks else 100
            conversation_stats.append({"agent_id": agent_id, "conversation_count": conversation_count})
            satisfaction_stats.append(
                {
                    "agent_id": agent_id,
                    "satisfaction_rate": satisfaction_rate,
                    "total_feedbacks": total_feedbacks,
                }
            )
            tool_usage.append({"agent_id": agent_id, "tool_usage_count": tool_counts.get(agent_id, 0)})
            top_agents.append(
                {
                    "agent_id": agent_id,
                    "agent_avatar": normalize_public_minio_url(agent.icon) if agent and agent.icon else None,
                    "conversation_count": conversation_count,
                    "satisfaction_rate": satisfaction_rate,
                }
            )

        top_agents.sort(key=lambda row: row["conversation_count"], reverse=True)
        return {
            "total_agents": len(agent_slugs),
            "agent_conversation_counts": conversation_stats,
            "agent_satisfaction_rates": satisfaction_stats,
            "agent_tool_usage": tool_usage,
            "top_performing_agents": top_agents[:5],
            "agent_names": {
                agent_id: agents_by_slug[agent_id].name if agent_id in agents_by_slug else agent_id
                for agent_id in agent_slugs
            },
        }

    async def get_basic_stats(
        self,
        *,
        conversation_filter: Any | None = None,
        feedback_filter: Any | None = None,
        tool_filter: Any | None = None,
        operation_filter: Any | None = None,
        audit_filter: Any | None = None,
        total_users: int | None = None,
    ) -> dict[str, Any]:
        """按历史组织快照读取 Dashboard 基础计数与满意度。"""

        conversation_filters = [Conversation.status.notin_(("deleted", "subagent"))]
        if conversation_filter is not None:
            conversation_filters.append(conversation_filter)
        feedback_filters = (feedback_filter,) if feedback_filter is not None else ()
        total_conversations_result = await self.db_session.execute(
            select(func.count(Conversation.id)).where(*conversation_filters)
        )
        active_conversations_result = await self.db_session.execute(
            select(func.count(Conversation.id)).where(Conversation.status == "active", *conversation_filters)
        )
        total_messages_result = await self.db_session.execute(
            select(func.count(Message.id))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(*conversation_filters)
        )
        if total_users is None:
            total_users_result = await self.db_session.execute(select(func.count(User.id)).where(User.is_deleted == 0))
            total_users = total_users_result.scalar() or 0
        total_feedbacks_result = await self.db_session.execute(
            select(func.count(MessageFeedback.id))
            .join(Message, MessageFeedback.message_id == Message.id)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.status.notin_(("deleted", "subagent")), *feedback_filters)
        )
        like_count_result = await self.db_session.execute(
            select(func.count(MessageFeedback.id))
            .join(Message, MessageFeedback.message_id == Message.id)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Conversation.status.notin_(("deleted", "subagent")),
                MessageFeedback.rating == "like",
                *feedback_filters,
            )
        )
        total_feedbacks = total_feedbacks_result.scalar() or 0
        like_count = like_count_result.scalar() or 0
        inferred_checks = (
            (Conversation, conversation_filter),
            (ToolCall, tool_filter),
            (MessageFeedback, feedback_filter),
            (OperationLog, operation_filter),
            (SecurityAudit, audit_filter),
        )
        contains_inferred_data = False
        for model, scope_filter in inferred_checks:
            if scope_filter is None:
                continue
            inferred_query = select(model.id)
            if model is ToolCall:
                inferred_query = inferred_query.join(Message, ToolCall.message_id == Message.id).join(
                    Conversation, Message.conversation_id == Conversation.id
                )
            inferred_id = await self.db_session.scalar(
                inferred_query.where(scope_filter, model.organization_snapshot_inferred.is_(True)).limit(1)
            )
            if inferred_id is not None:
                contains_inferred_data = True
                break

        return {
            "total_conversations": total_conversations_result.scalar() or 0,
            "active_conversations": active_conversations_result.scalar() or 0,
            "total_messages": total_messages_result.scalar() or 0,
            "total_users": total_users,
            "contains_inferred_data": contains_inferred_data,
            "feedback_stats": {
                "total_feedbacks": total_feedbacks,
                "satisfaction_rate": round(like_count / total_feedbacks * 100, 2) if total_feedbacks else 100,
            },
        }

    async def list_feedbacks(
        self,
        *,
        rating: str | None,
        agent_id: str | None,
        scope_filter: Any | None = None,
    ) -> list[tuple[MessageFeedback, Message, Conversation, User | None]]:
        """按可选评分和智能体过滤反馈关联数据。"""
        query = (
            select(MessageFeedback, Message, Conversation, User)
            .join(Message, MessageFeedback.message_id == Message.id)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .outerjoin(User, MessageFeedback.uid == User.uid)
            .where(Conversation.status.notin_(("deleted", "subagent")))
        )
        if scope_filter is not None:
            query = query.where(scope_filter)
        if rating and rating in {"like", "dislike"}:
            query = query.where(MessageFeedback.rating == rating)
        if agent_id:
            query = query.where(Conversation.agent_id == agent_id)
        query = query.order_by(MessageFeedback.created_at.desc())
        result = await self.db_session.execute(query)
        return list(result.all())

    async def get_call_timeseries(
        self,
        *,
        metric_type: str,
        time_range: str,
        conversation_filter: Any | None = None,
        tool_filter: Any | None = None,
        now: datetime | None = None,
        local_now: datetime | None = None,
    ) -> dict[str, Any]:
        """查询并补齐十四个时间区间的调用分析序列。"""
        query_now = now or utc_now()
        query_local_now = local_now or shanghai_now()
        intervals = 14

        if time_range == "14hours":
            start_time = query_now - timedelta(hours=intervals - 1)
            base_local_time = ensure_shanghai(start_time)
        elif time_range == "14weeks":
            base_local_time = query_local_now - timedelta(weeks=intervals - 1)
            base_local_time = base_local_time - timedelta(days=base_local_time.weekday())
            base_local_time = base_local_time.replace(hour=0, minute=0, second=0, microsecond=0)
            start_time = base_local_time.astimezone(UTC)
        else:
            start_time = query_now - timedelta(days=intervals - 1)
            base_local_time = ensure_shanghai(start_time)

        query_start_time = start_time.replace(tzinfo=None)
        message_group = self._time_group_format(Message.created_at, time_range)
        conversation_filters = [Conversation.status.notin_(("deleted", "subagent"))]
        tool_filters = [Conversation.status.notin_(("deleted", "subagent"))]
        if conversation_filter is not None:
            conversation_filters.append(conversation_filter)
        if tool_filter is not None:
            tool_filters.append(tool_filter)

        if metric_type == "models":
            category = cast(Message.extra_metadata["response_metadata"]["model_name"], String)
            query = select(
                message_group.label("date"),
                func.count(Message.id).label("count"),
                category.label("category"),
            ).join(Conversation, Message.conversation_id == Conversation.id)
            result = await self.db_session.execute(
                query.where(
                    Message.role == "assistant",
                    Message.created_at >= query_start_time,
                    Message.extra_metadata.isnot(None),
                    *conversation_filters,
                )
                .group_by(message_group, category)
                .order_by(message_group)
            )
            rows = list(result.all())
        elif metric_type == "agents":
            conversation_group = self._time_group_format(Conversation.updated_at, time_range)
            result = await self.db_session.execute(
                select(
                    conversation_group.label("date"),
                    func.count(Conversation.id).label("count"),
                    Conversation.agent_id.label("category"),
                )
                .where(
                    Conversation.updated_at.isnot(None),
                    Conversation.updated_at >= query_start_time,
                    *conversation_filters,
                )
                .group_by(conversation_group, Conversation.agent_id)
                .order_by(conversation_group)
            )
            rows = list(result.all())
        elif metric_type == "tokens":
            rows = []
            for token_name in ("input_tokens", "output_tokens"):
                query = select(
                    message_group.label("date"),
                    func.sum(
                        func.coalesce(
                            cast(cast(Message.extra_metadata["usage_metadata"][token_name], String), Integer),
                            0,
                        )
                    ).label("count"),
                    literal(token_name).label("category"),
                ).join(Conversation, Message.conversation_id == Conversation.id)
                result = await self.db_session.execute(
                    query.where(
                        Message.created_at >= query_start_time,
                        Message.extra_metadata.isnot(None),
                        Message.extra_metadata["usage_metadata"].isnot(None),
                        *conversation_filters,
                    )
                    .group_by(message_group)
                    .order_by(message_group)
                )
                rows.extend(result.all())
        elif metric_type == "tools":
            tool_group = self._time_group_format(ToolCall.created_at, time_range)
            result = await self.db_session.execute(
                select(
                    tool_group.label("date"),
                    func.count(ToolCall.id).label("count"),
                    ToolCall.tool_name.label("category"),
                )
                .join(Message, ToolCall.message_id == Message.id)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(ToolCall.created_at >= query_start_time, *tool_filters)
                .group_by(tool_group, ToolCall.tool_name)
                .order_by(tool_group)
            )
            rows = list(result.all())
        else:
            raise ValueError(f"不支持的 Dashboard 指标类型: {metric_type}")

        categories = sorted({row.category for row in rows if row.category})
        if not categories:
            categories = {
                "models": ["unknown_model"],
                "agents": ["unknown_agent"],
                "tokens": ["input_tokens", "output_tokens"],
                "tools": ["unknown_tool"],
            }[metric_type]

        agent_names = None
        if metric_type == "agents":
            agent_slugs = [category for category in categories if category]
            if agent_slugs:
                agent_names = {
                    agent.slug: agent.name
                    for agent in await AgentRepository(self.db_session).list_by_slugs(agent_slugs)
                }

        time_data: dict[str, dict[str, int]] = {}
        for row in rows:
            date_key = row.date
            if time_range == "14weeks":
                base_date = datetime.strptime(f"{date_key}-1", "%Y-%W-%w")
                iso_year, iso_week, _ = base_date.isocalendar()
                date_key = f"{iso_year}-{iso_week:02d}"
            time_data.setdefault(date_key, {})[row.category] = row.count

        if time_range == "14hours":
            delta = timedelta(hours=1)
        elif time_range == "14weeks":
            delta = timedelta(weeks=1)
        else:
            delta = timedelta(days=1)

        data = []
        current_time = base_local_time
        for _ in range(intervals):
            if time_range == "14hours":
                date_key = current_time.strftime("%Y-%m-%d %H:00")
            elif time_range == "14weeks":
                iso_year, iso_week, _ = current_time.isocalendar()
                date_key = f"{iso_year}-{iso_week:02d}"
            else:
                date_key = current_time.strftime("%Y-%m-%d")

            interval_data = dict(time_data.get(date_key, {}))
            interval_total = sum(interval_data.values())
            for category in categories:
                interval_data.setdefault(category, 0)
            data.append({"date": date_key, "data": interval_data, "total": interval_total})
            current_time += delta

        if metric_type == "tools":
            total_result = await self.db_session.execute(
                select(func.count(ToolCall.id))
                .join(Message, ToolCall.message_id == Message.id)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(*tool_filters)
            )
            total_count = total_result.scalar() or 0
        else:
            total_count = sum(item["total"] for item in data)
        peak = max(data, key=lambda item: item["total"]) if data else {"total": 0, "date": ""}
        return {
            "data": data,
            "categories": categories,
            "total_count": total_count,
            "average_count": round(total_count / intervals, 2) if intervals else 0,
            "peak_count": peak["total"],
            "peak_date": peak["date"],
            "agent_names": agent_names,
        }

    async def list_resources(self, model: Any) -> list[Any]:
        """读取一种 Dashboard 资源，权限汇总由调用方使用统一 ACL 完成。"""

        result = await self.db_session.scalars(select(model))
        return list(result)

    async def get_thread_analytics(
        self,
        *,
        time_range: str = "30days",
        agent_id: str | None = None,
        include_subagents: bool = False,
        scope_filter: Any | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """统计会话（Thread）汇总、每日趋势、消息深度、Agent 与用户分布。"""
        raw_now = now or utc_now()
        query_now = raw_now.astimezone(UTC).replace(tzinfo=None) if raw_now.tzinfo else raw_now
        days = {"7days": 7, "14days": 14, "30days": 30, "90days": 90}.get(time_range, 30)
        local_start_day = (query_now + timedelta(hours=8)).replace(hour=0, minute=0, second=0, microsecond=0)
        local_start_day -= timedelta(days=days - 1)
        query_start_time = local_start_day - timedelta(hours=8)

        status_filter = (
            Conversation.status != "deleted"
            if include_subagents
            else Conversation.status.notin_(("deleted", "subagent"))
        )
        conversation_filters = [
            Conversation.created_at.isnot(None),
            status_filter,
        ]
        if scope_filter is not None:
            conversation_filters.append(scope_filter)
        if agent_id:
            conversation_filters.append(Conversation.agent_id == agent_id)

        summary_result = await self.db_session.execute(
            select(
                func.count(Conversation.id).label("total_threads"),
                func.sum(case((Conversation.is_pinned.is_(True), 1), else_=0)).label("pinned_threads"),
            ).where(*conversation_filters)
        )
        summary_row = summary_result.one()

        message_summary_result = await self.db_session.execute(
            select(
                func.count(Message.id).label("total_messages"),
                func.count(
                    distinct(case((Message.created_at >= query_start_time, Message.conversation_id), else_=None))
                ).label("active_threads"),
            )
            .select_from(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(*conversation_filters)
        )
        message_summary_row = message_summary_result.one()

        tokens_query = (
            select(func.coalesce(func.sum(ConversationStats.total_tokens), 0))
            .select_from(ConversationStats)
            .join(Conversation, ConversationStats.conversation_id == Conversation.id)
            .where(*conversation_filters)
        )
        total_tokens = int((await self.db_session.execute(tokens_query)).scalar() or 0)

        total_threads = int(summary_row.total_threads or 0)
        active_threads = int(message_summary_row.active_threads or 0)
        pinned_threads = int(summary_row.pinned_threads or 0)
        total_messages = int(message_summary_row.total_messages or 0)

        new_thread_date = self._shanghai_date_group(Conversation.created_at)
        new_thread_rows = (
            await self.db_session.execute(
                select(new_thread_date.label("date"), func.count(Conversation.id).label("count"))
                .where(
                    *conversation_filters,
                    Conversation.created_at >= query_start_time,
                    Conversation.created_at <= query_now,
                )
                .group_by(new_thread_date)
            )
        ).all()
        new_threads_by_date = {str(row.date): int(row.count or 0) for row in new_thread_rows}

        message_date = self._shanghai_date_group(Message.created_at)
        activity_query = (
            select(
                message_date.label("date"),
                func.count(distinct(Message.conversation_id)).label("active_threads"),
                func.count(Message.id).label("message_count"),
            )
            .select_from(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Message.created_at >= query_start_time, Message.created_at <= query_now, *conversation_filters)
            .group_by(message_date)
        )
        activity_rows = (await self.db_session.execute(activity_query)).all()
        activity_by_date = {
            str(row.date): {
                "active_threads": int(row.active_threads or 0),
                "message_count": int(row.message_count or 0),
            }
            for row in activity_rows
        }

        daily_trends = []
        for day_offset in range(days):
            date_key = (local_start_day + timedelta(days=day_offset)).strftime("%Y-%m-%d")
            activity = activity_by_date.get(date_key, {})
            daily_trends.append(
                {
                    "date": date_key,
                    "new_threads": new_threads_by_date.get(date_key, 0),
                    "active_threads": activity.get("active_threads", 0),
                    "message_count": activity.get("message_count", 0),
                }
            )

        # 3. 消息深度分布 (0条, 1-2条, 3-5条, 6-10条, 11-20条, 20+条)
        depth_query = (
            select(
                func.sum(case((func.coalesce(ConversationStats.message_count, 0) == 0, 1), else_=0)).label("d0"),
                func.sum(
                    case(
                        (
                            (func.coalesce(ConversationStats.message_count, 0) >= 1)
                            & (func.coalesce(ConversationStats.message_count, 0) <= 2),
                            1,
                        ),
                        else_=0,
                    )
                ).label("d1_2"),
                func.sum(
                    case(
                        (
                            (func.coalesce(ConversationStats.message_count, 0) >= 3)
                            & (func.coalesce(ConversationStats.message_count, 0) <= 5),
                            1,
                        ),
                        else_=0,
                    )
                ).label("d3_5"),
                func.sum(
                    case(
                        (
                            (func.coalesce(ConversationStats.message_count, 0) >= 6)
                            & (func.coalesce(ConversationStats.message_count, 0) <= 10),
                            1,
                        ),
                        else_=0,
                    )
                ).label("d6_10"),
                func.sum(
                    case(
                        (
                            (func.coalesce(ConversationStats.message_count, 0) >= 11)
                            & (func.coalesce(ConversationStats.message_count, 0) <= 20),
                            1,
                        ),
                        else_=0,
                    )
                ).label("d11_20"),
                func.sum(case((func.coalesce(ConversationStats.message_count, 0) > 20, 1), else_=0)).label("d20_plus"),
            )
            .select_from(Conversation)
            .outerjoin(ConversationStats, Conversation.id == ConversationStats.conversation_id)
            .where(*conversation_filters)
        )
        depth_row = (await self.db_session.execute(depth_query)).one()
        depth_distribution = {
            "0 条": int(depth_row.d0 or 0),
            "1-2 条": int(depth_row.d1_2 or 0),
            "3-5 条": int(depth_row.d3_5 or 0),
            "6-10 条": int(depth_row.d6_10 or 0),
            "11-20 条": int(depth_row.d11_20 or 0),
            "20+ 条": int(depth_row.d20_plus or 0),
        }

        # 4. 各智能体会话分布
        agent_group_query = (
            select(
                Conversation.agent_id,
                func.count(Conversation.id).label("thread_count"),
                func.coalesce(func.sum(ConversationStats.message_count), 0).label("message_count"),
                func.coalesce(func.sum(ConversationStats.total_tokens), 0).label("token_count"),
            )
            .select_from(Conversation)
            .outerjoin(ConversationStats, Conversation.id == ConversationStats.conversation_id)
            .where(*conversation_filters)
            .group_by(Conversation.agent_id)
            .order_by(func.count(Conversation.id).desc())
        )
        agent_rows = (await self.db_session.execute(agent_group_query)).all()
        agent_slugs = [row.agent_id for row in agent_rows if row.agent_id]
        agent_names_map = {}
        agent_avatars_map = {}
        if agent_slugs:
            agents = await AgentRepository(self.db_session).list_by_slugs(agent_slugs)
            agent_names_map = {a.slug: a.name for a in agents}
            agent_avatars_map = {a.slug: normalize_public_minio_url(a.icon) if a.icon else None for a in agents}

        agent_distribution = [
            {
                "agent_id": row.agent_id,
                "agent_name": agent_names_map.get(row.agent_id, row.agent_id),
                "agent_avatar": agent_avatars_map.get(row.agent_id),
                "thread_count": int(row.thread_count or 0),
                "message_count": int(row.message_count or 0),
                "token_count": int(row.token_count or 0),
                "avg_messages": round(int(row.message_count or 0) / int(row.thread_count or 1), 1),
            }
            for row in agent_rows
        ]

        # 5. 高频用户活跃排行
        user_group_query = (
            select(
                Conversation.uid,
                User.username,
                User.display_name,
                User.avatar,
                func.count(Conversation.id).label("thread_count"),
                func.coalesce(func.sum(ConversationStats.message_count), 0).label("message_count"),
                func.max(Conversation.updated_at).label("last_active_at"),
            )
            .select_from(Conversation)
            .outerjoin(User, _conversation_user_join())
            .outerjoin(ConversationStats, Conversation.id == ConversationStats.conversation_id)
            .where(*conversation_filters)
            .group_by(Conversation.uid, User.username, User.display_name, User.avatar)
            .order_by(func.count(Conversation.id).desc())
            .limit(10)
        )
        user_rows = (await self.db_session.execute(user_group_query)).all()
        top_users = [
            {
                "uid": row.uid,
                "username": row.username or row.uid,
                "display_name": row.display_name,
                "avatar": normalize_public_minio_url(row.avatar) if row.avatar else None,
                "thread_count": int(row.thread_count or 0),
                "message_count": int(row.message_count or 0),
                "last_active_at": row.last_active_at.isoformat() if row.last_active_at else None,
            }
            for row in user_rows
        ]

        # 6. 状态分布
        status_query = (
            select(Conversation.status, func.count(Conversation.id))
            .where(*conversation_filters)
            .group_by(Conversation.status)
        )
        status_rows = (await self.db_session.execute(status_query)).all()
        status_distribution = {row[0] or "unknown": int(row[1] or 0) for row in status_rows}

        return {
            "summary": {
                "total_threads": total_threads,
                "active_threads": active_threads,
                "total_messages": total_messages,
                "total_tokens": total_tokens,
                "avg_messages_per_thread": round(total_messages / total_threads, 1) if total_threads else 0.0,
                "avg_tokens_per_thread": round(total_tokens / total_threads, 0) if total_threads else 0.0,
                "pinned_threads": pinned_threads,
            },
            "daily_trends": daily_trends,
            "depth_distribution": depth_distribution,
            "agent_distribution": agent_distribution,
            "top_users": top_users,
            "status_distribution": status_distribution,
        }
