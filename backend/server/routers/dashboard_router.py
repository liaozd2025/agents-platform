"""Dashboard 统计与监控 HTTP 路由。"""

import traceback
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.permissions.authorization import AuthorizationContext, AuthorizationTarget, parse_department_ancestor_ids
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.repositories.dashboard_repository import DashboardRepository
from yuxi.repositories.department_repository import DepartmentRepository
from yuxi.services.dashboard_scope_service import dashboard_history_filter, dashboard_tool_history_filter
from yuxi.services.dashboard_service import DashboardService
from yuxi.services.user_management_service import (
    department_is_accessible,
    list_authorized_departments,
    list_authorized_users,
)
from yuxi.storage.minio.client import normalize_public_minio_url
from yuxi.storage.postgres.models_business import (
    Conversation,
    MessageFeedback,
    OperationLog,
    SecurityAudit,
)
from yuxi.utils.logging_config import logger

from server.utils.auth_middleware import get_db, require_permission

dashboard = APIRouter(prefix="/dashboard", tags=["Dashboard"])


class UserActivityStats(BaseModel):
    """用户活跃度统计。"""

    total_users: int
    active_users_24h: int
    active_users_30d: int
    daily_active_users: list[dict]


class ToolCallStats(BaseModel):
    """工具调用统计。"""

    total_calls: int
    successful_calls: int
    failed_calls: int
    success_rate: float
    most_used_tools: list[dict]
    tool_error_distribution: dict
    daily_tool_calls: list[dict]


class ResourceScopeMetric(BaseModel):
    """一种资源的创建归属和共享可见统计。"""

    creation_count: int
    shared_visible_count: int
    contains_inferred_data: bool


class ResourceScopeStats(BaseModel):
    """知识库、智能体和 Skill 的组织口径统计。"""

    knowledge_bases: ResourceScopeMetric
    agents: ResourceScopeMetric
    skills: ResourceScopeMetric
    contains_inferred_data: bool


class AgentAnalytics(BaseModel):
    """智能体使用分析。"""

    total_agents: int
    agent_conversation_counts: list[dict]
    agent_satisfaction_rates: list[dict]
    agent_tool_usage: list[dict]
    top_performing_agents: list[dict]
    agent_names: dict[str, str] = {}


class ConversationListItem(BaseModel):
    """Dashboard 对话列表项。"""

    thread_id: str
    uid: str
    username: str | None = None
    display_name: str | None = None
    user_avatar: str | None = None
    user_deleted: bool = False
    agent_id: str
    agent_name: str | None = None
    agent_avatar: str | None = None
    agent_deleted: bool = False
    title: str | None
    status: str
    run_status: str | None = None
    is_pinned: bool = False
    message_count: int
    total_tokens: int = 0
    created_at: str
    updated_at: str


class ConversationListResponse(BaseModel):
    """会话分页列表响应。"""

    items: list[ConversationListItem]
    total: int
    limit: int
    offset: int


class ConversationFilterOption(BaseModel):
    """会话审计筛选选项。"""

    uid: str | None = None
    username: str | None = None
    display_name: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    avatar: str | None = None
    is_deleted: bool = False


class ConversationFilterOptionsResponse(BaseModel):
    """会话审计用户与 Agent 筛选项。"""

    users: list[ConversationFilterOption]
    agents: list[ConversationFilterOption]


class ConversationDetailResponse(BaseModel):
    """Dashboard 对话详情。"""

    thread_id: str
    uid: str
    username: str | None = None
    display_name: str | None = None
    user_avatar: str | None = None
    user_deleted: bool = False
    agent_id: str
    agent_name: str | None = None
    agent_avatar: str | None = None
    agent_deleted: bool = False
    title: str | None
    status: str
    run_status: str | None = None
    is_pinned: bool = False
    message_count: int
    created_at: str
    updated_at: str
    total_tokens: int
    messages: list[dict]


class DashboardDepartmentOption(BaseModel):
    """Dashboard 组织筛选项。"""

    id: int
    name: str
    parent_id: int | None
    node_type: str
    selectable: bool


class CurrentOrganizationStats(BaseModel):
    """当前组织关系下的人员和组织统计。"""

    selected_department_id: int | None
    selected_department_name: str
    includes_descendants: bool
    total_users: int
    total_departments: int
    departments: list[DashboardDepartmentOption]


class FeedbackListItem(BaseModel):
    """反馈列表项。"""

    id: int
    uid: str
    username: str | None
    avatar: str | None
    rating: str
    reason: str | None
    created_at: str
    message_content: str
    conversation_title: str | None
    agent_id: str


class TimeSeriesStats(BaseModel):
    """调用分析时间序列。"""

    data: list[dict]
    categories: list[str]
    total_count: int
    average_count: float
    peak_count: int
    peak_date: str
    agent_names: dict[str, str] | None = None


@dashboard.get("/stats/current-organization", response_model=CurrentOrganizationStats)
async def get_current_organization_stats(
    department_id: int | None = None,
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
    db: AsyncSession = Depends(get_db),
):
    """按查看者的数据范围统计当前组织子树。"""

    user_rows = await list_authorized_users(
        authorization,
        "dashboard:view",
        department_id=department_id,
        db=db,
    )
    if user_rows is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="组织节点不存在")

    departments = await list_authorized_departments(authorization, "dashboard:view", db=db)
    paths = await DepartmentRepository().get_paths_by_ids([item["id"] for item in departments], session=db)
    options = [
        {
            **item,
            "selectable": authorization.allows(
                "dashboard:view",
                AuthorizationTarget(department_ancestor_ids=parse_department_ancestor_ids(paths.get(item["id"]))),
            ),
        }
        for item in departments
    ]
    selected = next((item for item in options if item["id"] == department_id and item["selectable"]), None)
    selected_path = paths.get(department_id) if selected else None
    total_departments = sum(
        item["selectable"] and (selected_path is None or paths[item["id"]].startswith(selected_path))
        for item in options
    )
    return CurrentOrganizationStats(
        selected_department_id=department_id,
        selected_department_name=selected["name"] if selected else "全部授权组织",
        includes_descendants=True,
        total_users=len(user_rows),
        total_departments=total_departments,
        departments=[DashboardDepartmentOption(**item) for item in options],
    )


@dashboard.get("/conversations", response_model=ConversationListResponse)
async def get_all_conversations(
    uid: str | None = None,
    agent_id: str | None = None,
    status: Literal["active", "archived", "deleted", "subagent", "all"] = "active",
    search: Annotated[str | None, Query(max_length=255)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按事件组织快照获取可见对话。"""

    try:
        scope_filter = await dashboard_history_filter(
            db,
            authorization,
            Conversation.organization_path_snapshot,
            department_id,
            owner_uid_column=Conversation.uid,
        )
        return await DashboardService(db).list_conversations(
            uid=uid,
            agent_id=agent_id,
            status=status,
            search=search,
            limit=limit,
            offset=offset,
            scope_filter=scope_filter,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error getting conversations: {exc}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get conversations: {exc}") from exc


@dashboard.get("/conversations/options", response_model=ConversationFilterOptionsResponse)
async def get_conversation_filter_options(
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """获取当前历史组织范围内的会话筛选项。"""

    scope_filter = await dashboard_history_filter(
        db,
        authorization,
        Conversation.organization_path_snapshot,
        department_id,
        owner_uid_column=Conversation.uid,
    )
    return await DashboardService(db).get_conversation_filter_options(scope_filter=scope_filter)


@dashboard.get("/conversations/{thread_id}", response_model=ConversationDetailResponse)
async def get_conversation_detail(
    thread_id: str,
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """获取管理域内的指定对话详情。"""

    try:
        repository = ConversationRepository(db)
        conversation = await repository.get_conversation_by_thread_id(thread_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

        target = AuthorizationTarget(
            owner_user_id=authorization.user.id if conversation.uid == authorization.user.uid else None,
            department_ancestor_ids=parse_department_ancestor_ids(conversation.organization_path_snapshot),
        )
        selected_matches = department_id is None or department_id in target.department_ancestor_ids
        if not selected_matches or not authorization.allows("dashboard:view", target):
            raise HTTPException(status_code=404, detail="Conversation not found")
        if department_id is not None and not await department_is_accessible(
            authorization,
            "dashboard:view",
            department_id,
            db=db,
        ):
            raise HTTPException(status_code=404, detail="Conversation not found")

        data = await DashboardService(db).get_conversation_detail(thread_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return data
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error getting conversation detail: {exc}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get conversation detail: {exc}") from exc


@dashboard.get("/stats/users", response_model=UserActivityStats)
async def get_user_activity_stats(
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按历史组织快照获取用户活动统计。"""

    try:
        scope_filter = await dashboard_history_filter(
            db,
            authorization,
            Conversation.organization_path_snapshot,
            department_id,
            owner_uid_column=Conversation.uid,
        )
        current_users = await list_authorized_users(
            authorization,
            "dashboard:view",
            department_id=department_id,
            db=db,
        )
        if current_users is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="组织节点不存在")
        return UserActivityStats(
            **await DashboardRepository(db).get_user_activity_stats(
                scope_filter=scope_filter,
                total_users=len(current_users),
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error getting user activity stats: {exc}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get user activity stats: {exc}") from exc


@dashboard.get("/stats/tools", response_model=ToolCallStats)
async def get_tool_call_stats(
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按历史组织快照获取工具调用统计。"""

    try:
        scope_filter = await dashboard_tool_history_filter(db, authorization, department_id)
        return ToolCallStats(**await DashboardRepository(db).get_tool_call_stats(scope_filter=scope_filter))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error getting tool call stats: {exc}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get tool call stats: {exc}") from exc


@dashboard.get("/stats/resources", response_model=ResourceScopeStats)
async def get_resource_scope_stats(
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """分别统计资源创建组织和当前共享可见范围。"""

    return ResourceScopeStats(
        **await DashboardService(db).get_resource_scope_stats(
            authorization=authorization,
            department_id=department_id,
        )
    )


@dashboard.get("/stats/agents", response_model=AgentAnalytics)
async def get_agent_analytics(
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按历史组织快照获取智能体分析。"""

    try:
        conversation_filter = await dashboard_history_filter(
            db,
            authorization,
            Conversation.organization_path_snapshot,
            department_id,
            owner_uid_column=Conversation.uid,
        )
        feedback_filter = await dashboard_history_filter(
            db,
            authorization,
            MessageFeedback.organization_path_snapshot,
            department_id,
            owner_uid_column=MessageFeedback.uid,
        )
        tool_filter = await dashboard_tool_history_filter(db, authorization, department_id)
        return AgentAnalytics(
            **await DashboardRepository(db).get_agent_analytics(
                conversation_filter=conversation_filter,
                feedback_filter=feedback_filter,
                tool_filter=tool_filter,
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error getting agent analytics: {exc}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get agent analytics: {exc}") from exc


@dashboard.get("/stats")
async def get_dashboard_stats(
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按当前人员关系和历史事件快照获取基础统计。"""

    try:
        conversation_filter = await dashboard_history_filter(
            db,
            authorization,
            Conversation.organization_path_snapshot,
            department_id,
            owner_uid_column=Conversation.uid,
        )
        feedback_filter = await dashboard_history_filter(
            db,
            authorization,
            MessageFeedback.organization_path_snapshot,
            department_id,
            owner_uid_column=MessageFeedback.uid,
        )
        tool_filter = await dashboard_tool_history_filter(db, authorization, department_id)
        operation_filter = await dashboard_history_filter(
            db,
            authorization,
            OperationLog.organization_path_snapshot,
            department_id,
            owner_user_id_column=OperationLog.user_id,
        )
        audit_filter = await dashboard_history_filter(
            db,
            authorization,
            SecurityAudit.organization_path_snapshot,
            department_id,
            owner_user_id_column=SecurityAudit.actor_user_id,
        )
        current_users = await list_authorized_users(
            authorization,
            "dashboard:view",
            department_id=department_id,
            db=db,
        )
        if current_users is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="组织节点不存在")
        return await DashboardRepository(db).get_basic_stats(
            conversation_filter=conversation_filter,
            feedback_filter=feedback_filter,
            tool_filter=tool_filter,
            operation_filter=operation_filter,
            audit_filter=audit_filter,
            total_users=len(current_users),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error getting dashboard stats: {exc}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get dashboard stats: {exc}") from exc


@dashboard.get("/feedbacks", response_model=list[FeedbackListItem])
async def get_all_feedbacks(
    rating: str | None = None,
    agent_id: str | None = None,
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按历史组织快照获取反馈记录。"""

    try:
        scope_filter = await dashboard_history_filter(
            db,
            authorization,
            MessageFeedback.organization_path_snapshot,
            department_id,
            owner_uid_column=MessageFeedback.uid,
        )
        results = await DashboardRepository(db).list_feedbacks(
            rating=rating,
            agent_id=agent_id,
            scope_filter=scope_filter,
        )
        logger.info(f"Found {len(results)} feedback records")
        return [
            {
                "id": feedback.id,
                "message_id": feedback.message_id,
                "uid": feedback.uid,
                "username": user.username if user else None,
                "avatar": normalize_public_minio_url(user.avatar) if user else None,
                "rating": feedback.rating,
                "reason": feedback.reason,
                "created_at": feedback.created_at.isoformat(),
                "message_content": message.content,
                "conversation_title": conversation.title,
                "agent_id": conversation.agent_id,
            }
            for feedback, message, conversation, user in results
        ]
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error getting feedbacks: {exc}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get feedbacks: {exc}") from exc


class ThreadSummary(BaseModel):
    """会话汇总指标。"""

    total_threads: int
    active_threads: int
    total_messages: int
    total_tokens: int
    avg_messages_per_thread: float
    avg_tokens_per_thread: float
    pinned_threads: int = 0


class ThreadDailyTrend(BaseModel):
    """每日会话趋势。"""

    date: str
    new_threads: int
    active_threads: int
    message_count: int


class ThreadAgentStat(BaseModel):
    """智能体会话分布指标。"""

    agent_id: str
    agent_name: str
    thread_count: int
    message_count: int
    token_count: int
    avg_messages: float
    agent_avatar: str | None = None


class ThreadUserStat(BaseModel):
    """高频用户统计项。"""

    uid: str
    username: str | None
    display_name: str | None = None
    avatar: str | None
    thread_count: int
    message_count: int
    last_active_at: str | None


class ThreadAnalyticsResponse(BaseModel):
    """会话多维分析响应模型。"""

    summary: ThreadSummary
    daily_trends: list[ThreadDailyTrend]
    depth_distribution: dict[str, int]
    agent_distribution: list[ThreadAgentStat]
    top_users: list[ThreadUserStat]
    status_distribution: dict[str, int]


@dashboard.get("/stats/calls/timeseries", response_model=TimeSeriesStats)
async def get_call_timeseries_stats(
    type: Literal["models", "agents", "tokens", "tools"] = "models",
    time_range: Literal["14hours", "14days", "14weeks"] = "14days",
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按历史组织快照获取调用分析时间序列。"""

    try:
        conversation_filter = await dashboard_history_filter(
            db,
            authorization,
            Conversation.organization_path_snapshot,
            department_id,
            owner_uid_column=Conversation.uid,
        )
        tool_filter = await dashboard_tool_history_filter(db, authorization, department_id)
        return TimeSeriesStats(
            **await DashboardRepository(db).get_call_timeseries(
                metric_type=type,
                time_range=time_range,
                conversation_filter=conversation_filter,
                tool_filter=tool_filter,
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error getting call timeseries stats: {exc}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get call timeseries stats: {exc}") from exc


@dashboard.get("/stats/threads", response_model=ThreadAnalyticsResponse)
async def get_thread_analytics_stats(
    time_range: Literal["7days", "14days", "30days", "90days"] = "30days",
    agent_id: str | None = None,
    include_subagents: bool = Query(False, description="是否将子智能体会话纳入统计"),
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按历史组织快照获取会话多维分析统计。"""
    scope_filter = await dashboard_history_filter(
        db,
        authorization,
        Conversation.organization_path_snapshot,
        department_id,
        owner_uid_column=Conversation.uid,
    )
    data = await DashboardService(db).get_thread_analytics(
        time_range=time_range,
        agent_id=agent_id,
        include_subagents=include_subagents,
        scope_filter=scope_filter,
    )
    return ThreadAnalyticsResponse(**data)
