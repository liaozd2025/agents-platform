"""Dashboard 统计读模型的数据访问层。"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Integer, String, cast, distinct, func, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.repositories.agent_repository import AgentRepository
from yuxi.storage.postgres.models_business import (
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

    async def list_conversations(
        self,
        *,
        uid: str | None,
        agent_id: str | None,
        status: str,
        limit: int,
        offset: int,
        scope_filter: Any | None = None,
    ) -> list[dict[str, Any]]:
        """查询并组装 Dashboard 对话列表。"""
        query = select(Conversation, ConversationStats).outerjoin(
            ConversationStats, Conversation.id == ConversationStats.conversation_id
        )
        if scope_filter is not None:
            query = query.where(scope_filter)
        if uid:
            query = query.where(Conversation.uid == uid)
        if agent_id:
            query = query.where(Conversation.agent_id == agent_id)
        if status != "all":
            query = query.where(Conversation.status == status)
        query = query.order_by(Conversation.updated_at.desc()).limit(limit).offset(offset)

        result = await self.db_session.execute(query)
        return [
            {
                "thread_id": conversation.thread_id,
                "uid": conversation.uid,
                "agent_id": conversation.agent_id,
                "title": conversation.title,
                "status": conversation.status,
                "message_count": stats.message_count if stats else 0,
                "created_at": conversation.created_at.isoformat(),
                "updated_at": conversation.updated_at.isoformat(),
            }
            for conversation, stats in result.all()
        ]

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
        if total_users is None:
            total_result = await self.db_session.execute(select(func.count(User.id)).where(User.is_deleted == 0))
            total_users = total_result.scalar() or 0
        active_24h_result = await self.db_session.execute(
            select(func.count(distinct(User.id)))
            .select_from(Conversation)
            .join(User, Conversation.uid == User.uid)
            .where(Conversation.updated_at >= query_now - timedelta(days=1), User.is_deleted == 0, *filters)
        )
        active_30d_result = await self.db_session.execute(
            select(func.count(distinct(User.id)))
            .select_from(Conversation)
            .join(User, Conversation.uid == User.uid)
            .where(Conversation.updated_at >= query_now - timedelta(days=30), User.is_deleted == 0, *filters)
        )

        daily_active_users = []
        for day_offset in range(7):
            day_start = query_now - timedelta(days=day_offset + 1)
            day_end = query_now - timedelta(days=day_offset)
            active_result = await self.db_session.execute(
                select(func.count(distinct(User.id)))
                .select_from(Conversation)
                .join(User, Conversation.uid == User.uid)
                .where(
                    Conversation.updated_at >= day_start,
                    Conversation.updated_at < day_end,
                    User.is_deleted == 0,
                    *filters,
                )
            )
            daily_active_users.append(
                {"date": day_start.strftime("%Y-%m-%d"), "active_users": active_result.scalar() or 0}
            )

        return {
            "total_users": total_users,
            "active_users_24h": active_24h_result.scalar() or 0,
            "active_users_30d": active_30d_result.scalar() or 0,
            "daily_active_users": list(reversed(daily_active_users)),
        }

    async def get_tool_call_stats(
        self,
        *,
        scope_filter: Any | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """统计工具调用总量、成功率、排行与近七日趋势。"""
        query_now = (now or utc_now()).replace(tzinfo=None)
        filters = (scope_filter,) if scope_filter is not None else ()
        total_result = await self.db_session.execute(select(func.count(ToolCall.id)).where(*filters))
        successful_result = await self.db_session.execute(
            select(func.count(ToolCall.id)).where(ToolCall.status == "success", *filters)
        )
        total_calls = total_result.scalar() or 0
        successful_calls = successful_result.scalar() or 0

        most_used_result = await self.db_session.execute(
            select(ToolCall.tool_name, func.count(ToolCall.id).label("count"))
            .where(*filters)
            .group_by(ToolCall.tool_name)
            .order_by(func.count(ToolCall.id).desc())
            .limit(10)
        )
        error_result = await self.db_session.execute(
            select(ToolCall.tool_name, func.count(ToolCall.id).label("error_count"))
            .where(ToolCall.status == "error", *filters)
            .group_by(ToolCall.tool_name)
        )

        daily_tool_calls = []
        for day_offset in range(7):
            day_start = query_now - timedelta(days=day_offset + 1)
            day_end = query_now - timedelta(days=day_offset)
            daily_result = await self.db_session.execute(
                select(func.count(ToolCall.id)).where(
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
        conversation_filters = (conversation_filter,) if conversation_filter is not None else ()
        feedback_filters = (feedback_filter,) if feedback_filter is not None else ()
        tool_filters = (tool_filter,) if tool_filter is not None else ()
        agents_result = await self.db_session.execute(
            select(Conversation.agent_id, func.count(Conversation.id).label("conversation_count"))
            .where(*conversation_filters)
            .group_by(Conversation.agent_id)
        )
        agents = list(agents_result.all())
        satisfaction = []
        tool_usage = []

        for agent_id, _ in agents:
            total_feedback_result = await self.db_session.execute(
                select(func.count(MessageFeedback.id))
                .join(Message, MessageFeedback.message_id == Message.id)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.agent_id == agent_id, *feedback_filters)
            )
            positive_feedback_result = await self.db_session.execute(
                select(func.count(MessageFeedback.id))
                .join(Message, MessageFeedback.message_id == Message.id)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.agent_id == agent_id, MessageFeedback.rating == "like", *feedback_filters)
            )
            total_feedbacks = total_feedback_result.scalar() or 0
            positive_feedbacks = positive_feedback_result.scalar() or 0
            satisfaction.append(
                {
                    "agent_id": agent_id,
                    "satisfaction_rate": (
                        round(positive_feedbacks / total_feedbacks * 100, 2) if total_feedbacks else 100
                    ),
                    "total_feedbacks": total_feedbacks,
                }
            )

            tool_usage_result = await self.db_session.execute(
                select(func.count(ToolCall.id))
                .join(Message, ToolCall.message_id == Message.id)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.agent_id == agent_id, *tool_filters)
            )
            tool_usage.append({"agent_id": agent_id, "tool_usage_count": tool_usage_result.scalar() or 0})

        top_agents = [
            {
                "agent_id": agent_id,
                "conversation_count": conversation_count,
                "satisfaction_rate": next(
                    (row["satisfaction_rate"] for row in satisfaction if row["agent_id"] == agent_id), 0
                ),
            }
            for agent_id, conversation_count in agents
        ]
        top_agents.sort(key=lambda row: row["conversation_count"], reverse=True)

        agent_slugs = [agent_id for agent_id, _ in agents if agent_id]
        agent_names = {}
        if agent_slugs:
            agent_names = {
                agent.slug: agent.name for agent in await AgentRepository(self.db_session).list_by_slugs(agent_slugs)
            }

        return {
            "total_agents": len(agents),
            "agent_conversation_counts": [
                {"agent_id": agent_id, "conversation_count": count} for agent_id, count in agents
            ],
            "agent_satisfaction_rates": satisfaction,
            "agent_tool_usage": tool_usage,
            "top_performing_agents": top_agents[:5],
            "agent_names": agent_names,
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

        conversation_filters = (conversation_filter,) if conversation_filter is not None else ()
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
            select(func.count(MessageFeedback.id)).where(*feedback_filters)
        )
        like_count_result = await self.db_session.execute(
            select(func.count(MessageFeedback.id)).where(MessageFeedback.rating == "like", *feedback_filters)
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
            inferred_id = await self.db_session.scalar(
                select(model.id).where(scope_filter, model.organization_snapshot_inferred.is_(True)).limit(1)
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
        conversation_filters = (conversation_filter,) if conversation_filter is not None else ()
        tool_filters = (tool_filter,) if tool_filter is not None else ()

        if metric_type == "models":
            category = cast(Message.extra_metadata["response_metadata"]["model_name"], String)
            query = select(
                message_group.label("date"),
                func.count(Message.id).label("count"),
                category.label("category"),
            )
            if conversation_filter is not None:
                query = query.join(Conversation, Message.conversation_id == Conversation.id)
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
                )
                if conversation_filter is not None:
                    query = query.join(Conversation, Message.conversation_id == Conversation.id)
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
            total_result = await self.db_session.execute(select(func.count(ToolCall.id)).where(*tool_filters))
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
