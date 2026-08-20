"""Dashboard 统计与监控 HTTP 路由。"""

import traceback

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.config.runtime import knowledge_capability_enabled
from yuxi.permissions import (
    ResourcePermission,
    resolve_agent_permission,
    resolve_knowledge_base_permission,
    resolve_skill_permission,
)
from yuxi.permissions.authorization import AuthorizationContext, AuthorizationTarget, parse_department_ancestor_ids
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.repositories.dashboard_repository import DashboardRepository
from yuxi.repositories.department_repository import DepartmentRepository
from yuxi.services.dashboard_scope_service import dashboard_history_filter, dashboard_resource_subjects
from yuxi.services.user_management_service import (
    department_is_accessible,
    list_authorized_departments,
    list_authorized_users,
)
from yuxi.storage.minio.client import normalize_public_minio_url
from yuxi.storage.postgres.models_business import (
    Agent,
    Conversation,
    MessageFeedback,
    OperationLog,
    SecurityAudit,
    Skill,
    ToolCall,
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
    agent_id: str
    title: str | None
    status: str
    message_count: int
    created_at: str
    updated_at: str


class ConversationDetailResponse(BaseModel):
    """Dashboard 对话详情。"""

    thread_id: str
    uid: str
    agent_id: str
    title: str | None
    status: str
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


@dashboard.get("/conversations", response_model=list[ConversationListItem])
async def get_all_conversations(
    uid: str | None = None,
    agent_id: str | None = None,
    status: str = "active",
    limit: int = 100,
    offset: int = 0,
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
        return await DashboardRepository(db).list_conversations(
            uid=uid,
            agent_id=agent_id,
            status=status,
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

        messages = await repository.get_messages(conversation.id)
        stats = await repository.get_stats(conversation.id)
        message_list = []
        for message in messages:
            message_data = {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "message_type": message.message_type,
                "created_at": message.created_at.isoformat(),
            }
            if message.tool_calls:
                message_data["tool_calls"] = [
                    {
                        "id": tool_call.id,
                        "tool_name": tool_call.tool_name,
                        "tool_input": tool_call.tool_input,
                        "tool_output": tool_call.tool_output,
                        "status": tool_call.status,
                    }
                    for tool_call in message.tool_calls
                ]
            message_list.append(message_data)

        return {
            "thread_id": conversation.thread_id,
            "uid": conversation.uid,
            "agent_id": conversation.agent_id,
            "title": conversation.title,
            "status": conversation.status,
            "message_count": stats.message_count if stats else len(message_list),
            "created_at": conversation.created_at.isoformat(),
            "updated_at": conversation.updated_at.isoformat(),
            "total_tokens": stats.total_tokens if stats else 0,
            "messages": message_list,
        }
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
        scope_filter = await dashboard_history_filter(
            db,
            authorization,
            ToolCall.organization_path_snapshot,
            department_id,
        )
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

    subjects = await dashboard_resource_subjects(db, authorization, department_id)
    repository = DashboardRepository(db)

    async def summarize(model, resolver) -> ResourceScopeMetric:
        """按资源主键去重后汇总一种资源。"""

        created_resources = []
        shared_resources = []
        # ponytail: 直接复用现有 ACL；资源或组织主体量出现慢查询后再下推为 SQL。
        for resource in await repository.list_resources(model):
            ancestor_ids = parse_department_ancestor_ids(resource.organization_path_snapshot)
            target = AuthorizationTarget(
                owner_user_id=authorization.user.id if resource.created_by == authorization.user.uid else None,
                department_ancestor_ids=ancestor_ids,
            )
            if (department_id is None or department_id in ancestor_ids) and authorization.allows(
                "dashboard:view", target
            ):
                created_resources.append(resource)
            if any(resolver(subject, resource) != ResourcePermission.NONE for subject in subjects):
                shared_resources.append(resource)

        return ResourceScopeMetric(
            creation_count=len(created_resources),
            shared_visible_count=len(shared_resources),
            contains_inferred_data=any(
                resource.organization_snapshot_inferred for resource in created_resources + shared_resources
            ),
        )

    if knowledge_capability_enabled():
        from yuxi.storage.postgres.models_knowledge import KnowledgeBase

        knowledge_bases = await summarize(KnowledgeBase, resolve_knowledge_base_permission)
    else:
        knowledge_bases = ResourceScopeMetric(
            creation_count=0,
            shared_visible_count=0,
            contains_inferred_data=False,
        )
    agents = await summarize(Agent, resolve_agent_permission)
    skills = await summarize(Skill, resolve_skill_permission)
    return ResourceScopeStats(
        knowledge_bases=knowledge_bases,
        agents=agents,
        skills=skills,
        contains_inferred_data=any(metric.contains_inferred_data for metric in (knowledge_bases, agents, skills)),
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
        tool_filter = await dashboard_history_filter(
            db,
            authorization,
            ToolCall.organization_path_snapshot,
            department_id,
        )
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
        tool_filter = await dashboard_history_filter(
            db,
            authorization,
            ToolCall.organization_path_snapshot,
            department_id,
        )
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


@dashboard.get("/stats/calls/timeseries", response_model=TimeSeriesStats)
async def get_call_timeseries_stats(
    type: str = "models",
    time_range: str = "14days",
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按历史组织快照获取调用分析时间序列。"""

    if type not in {"models", "agents", "tokens", "tools"}:
        raise HTTPException(status_code=422, detail=f"Invalid type: {type}")

    try:
        conversation_filter = await dashboard_history_filter(
            db,
            authorization,
            Conversation.organization_path_snapshot,
            department_id,
            owner_uid_column=Conversation.uid,
        )
        tool_filter = await dashboard_history_filter(
            db,
            authorization,
            ToolCall.organization_path_snapshot,
            department_id,
        )
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
