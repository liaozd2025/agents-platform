"""Dashboard 统计与监控 HTTP 路由。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.api.dependencies.auth import get_db, require_permission
from yuxi.modules.agents.services.sandbox import get_vm_stats
from yuxi.modules.identity.permissions.authorization import AuthorizationContext, AuthorizationTarget, parse_department_ancestor_ids
from yuxi.modules.identity.repositories.departments import DepartmentRepository
from yuxi.modules.identity.services.visibility import list_authorized_departments, list_authorized_users
from yuxi.modules.system.services.dashboard import DashboardService
from yuxi.modules.system.services.dashboard_scope import dashboard_query_scope

dashboard = APIRouter(prefix="/dashboard", tags=["Dashboard"])


class UserActivityStats(BaseModel):
    """用户活跃度统计。"""

    total_users: int
    active_users_24h: int
    active_users_30d: int
    daily_active_users: list[dict]


class VmStats(BaseModel):
    """执行节点实时 VM 数量。"""

    active_vms: int
    total_vms: int


class ToolCallStats(BaseModel):
    """工具调用统计。"""

    total_calls: int
    successful_calls: int
    failed_calls: int
    success_rate: float
    most_used_tools: list[dict]
    tool_error_distribution: dict
    daily_tool_calls: list[dict]


class AgentAnalytics(BaseModel):
    """AI 智能体分析。"""

    total_agents: int
    agent_session_counts: list[dict]
    agent_tool_usage: list[dict]
    agent_names: dict[str, str] = {}


class SessionListItem(BaseModel):
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
    total_tokens: int | None = None
    token_usage_complete: bool = False
    created_at: str
    updated_at: str


class SessionListResponse(BaseModel):
    """会话分页列表响应。"""

    items: list[SessionListItem]
    total: int
    limit: int
    offset: int


class SessionFilterOption(BaseModel):
    """会话审计筛选选项。"""

    uid: str | None = None
    username: str | None = None
    display_name: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    avatar: str | None = None
    is_deleted: bool = False


class SessionFilterOptionsResponse(BaseModel):
    """会话审计用户与 Agent 筛选项。"""

    users: list[SessionFilterOption]
    agents: list[SessionFilterOption]


class SessionDetailResponse(BaseModel):
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
    total_tokens: int | None
    token_usage_complete: bool = False
    messages: list[dict]


class TimeSeriesStats(BaseModel):
    """时间序列统计数据。"""

    data: list[dict]
    categories: list[str]
    total_count: int | None
    average_count: float | None
    peak_count: int | None
    peak_date: str
    agent_names: dict[str, str] | None = None
    token_usage_complete: bool | None = None
    run_count: int | None = None
    incomplete_run_count: int | None = None


class ThreadSummary(BaseModel):
    """会话汇总指标。"""

    total_threads: int
    active_threads: int
    total_messages: int
    total_tokens: int | None
    token_usage_complete: bool = False
    avg_messages_per_thread: float
    avg_tokens_per_thread: float | None
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
    token_count: int | None
    token_usage_complete: bool = False
    avg_messages: float
    agent_avatar: str | None = None


class AgentSessionStat(BaseModel):
    """所选创建范围内的单个智能体会话指标。"""

    agent_id: str
    agent_name: str
    thread_count: int
    message_count: int
    avg_messages: float


class AgentSessionDistribution(BaseModel):
    """智能体会话分布与同口径摘要。"""

    total_threads: int
    agent_count: int
    avg_messages_per_thread: float
    agent_distribution: list[AgentSessionStat]


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


async def _scoped_dashboard(
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
) -> DashboardService:
    """在 HTTP 边界解析数据范围，统计与详情沿各自事件快照拒绝越域读取。"""
    return DashboardService(db, **await dashboard_query_scope(db, authorization, department_id))


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


@dashboard.get("/stats")
async def get_dashboard_stats(
    service: DashboardService = Depends(_scoped_dashboard),
):
    """获取基础统计指标（按授权组织范围）。"""
    return await service.get_basic_stats()


@dashboard.get("/stats/users", response_model=UserActivityStats)
async def get_user_activity_stats(
    service: DashboardService = Depends(_scoped_dashboard),
):
    """获取用户活动统计（按授权组织范围）。"""
    return UserActivityStats(**await service.get_user_activity_stats())


@dashboard.get("/stats/vms", response_model=VmStats)
async def get_dashboard_vm_stats(authorization: AuthorizationContext = Depends(require_permission("system_config:manage"))):
    """仅向系统配置管理员返回实时计数，不触发 VM 生命周期动作。"""
    return await get_vm_stats()


@dashboard.get("/stats/tools", response_model=ToolCallStats)
async def get_tool_call_stats(
    service: DashboardService = Depends(_scoped_dashboard),
):
    """获取工具调用统计（按授权组织范围）。"""
    return ToolCallStats(**await service.get_tool_call_stats())


@dashboard.get("/stats/agents", response_model=AgentAnalytics)
async def get_agent_analytics(
    service: DashboardService = Depends(_scoped_dashboard),
):
    """获取智能体分析（按授权组织范围）。"""
    return AgentAnalytics(**await service.get_agent_analytics())


@dashboard.get("/stats/calls/timeseries", response_model=TimeSeriesStats)
async def get_call_timeseries_stats(
    type: Literal["models", "agents", "tokens", "tools", "sessions"] = "models",
    time_range: Literal["14hours", "14days", "14weeks"] = "14days",
    service: DashboardService = Depends(_scoped_dashboard),
):
    """获取调用分析时间序列统计（按授权组织范围）。"""
    data = await service.get_call_timeseries(
        metric_type=type,
        time_range=time_range,
    )
    return TimeSeriesStats(**data)


@dashboard.get("/stats/agents/sessions", response_model=AgentSessionDistribution)
async def get_agent_session_distribution(
    time_range: Literal["all", "7days", "30days", "3months"] = "all",
    service: DashboardService = Depends(_scoped_dashboard),
):
    """按会话创建范围读取分布，按授权组织范围读取。"""
    return await service.get_agent_session_distribution(time_range=time_range)


@dashboard.get("/stats/threads", response_model=ThreadAnalyticsResponse)
async def get_thread_analytics_stats(
    time_range: Literal["7days", "14days", "30days", "90days"] = "30days",
    agent_id: str | None = None,
    service: DashboardService = Depends(_scoped_dashboard),
):
    """获取会话多维分析统计（按授权组织范围）。"""
    data = await service.get_thread_analytics(
        time_range=time_range,
        agent_id=agent_id,
    )
    return ThreadAnalyticsResponse(**data)


@dashboard.get("/sessions/options", response_model=SessionFilterOptionsResponse)
async def get_session_filter_options(
    service: DashboardService = Depends(_scoped_dashboard),
):
    """获取会话审计用户与 Agent 筛选项（按授权组织范围）。"""
    return await service.get_session_filter_options()


@dashboard.get("/sessions", response_model=SessionListResponse)
async def get_all_sessions(
    uid: str | None = None,
    agent_id: str | None = None,
    status: Literal["active", "archived", "deleted", "all"] = "all",
    search: Annotated[str | None, Query(max_length=255)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    service: DashboardService = Depends(_scoped_dashboard),
):
    """获取所有对话（按授权组织范围）。"""
    return await service.list_sessions(
        uid=uid,
        agent_id=agent_id,
        status=status,
        search=search,
        limit=limit,
        offset=offset,
    )


@dashboard.get("/sessions/{thread_id}", response_model=SessionDetailResponse)
async def get_session_detail(
    thread_id: str,
    service: DashboardService = Depends(_scoped_dashboard),
):
    """获取指定对话详情（按授权组织范围）。"""
    data = await service.get_session_detail(thread_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found")
    return data


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
        item["selectable"] and (selected_path is None or paths[item["id"]].startswith(selected_path)) for item in options
    )
    return CurrentOrganizationStats(
        selected_department_id=department_id,
        selected_department_name=selected["name"] if selected else "全部授权组织",
        includes_descendants=True,
        total_users=len(user_rows),
        total_departments=total_departments,
        departments=[DashboardDepartmentOption(**item) for item in options],
    )


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
