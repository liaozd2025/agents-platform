"""
Dashboard Router - Statistics and monitoring endpoints
仪表板 - 统计和监控端点

Provides centralized dashboard APIs for monitoring system-wide statistics.
提供系统级统计和监控的API接口，用于监控系统运行状态、用户活动、工具调用、知识库使用等。
"""

import traceback
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Integer, String, and_, cast, distinct, false, func, or_, select, text, true
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, require_permission
from yuxi.permissions import (
    ResourcePermission,
    resolve_agent_permission,
    resolve_knowledge_base_permission,
    resolve_skill_permission,
)
from yuxi.permissions.authorization import AuthorizationContext, AuthorizationTarget, parse_department_ancestor_ids
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.repositories.department_repository import DepartmentRepository
from yuxi.services.user_management_service import (
    department_is_accessible,
    list_authorized_departments,
    list_authorized_users,
)
from yuxi.storage.minio.client import normalize_public_minio_url
from yuxi.storage.postgres.models_business import Agent, Skill
from yuxi.storage.postgres.models_knowledge import KnowledgeBase
from yuxi.utils.datetime_utils import UTC, ensure_shanghai, shanghai_now, utc_now
from yuxi.utils.logging_config import logger


dashboard = APIRouter(prefix="/dashboard", tags=["Dashboard"])


def _historical_visibility_filter(
    authorization: AuthorizationContext,
    path_column,
    *,
    owner_user_id_column=None,
    owner_uid_column=None,
):
    """把同一角色分配的 Dashboard 数据范围转换为历史快照 SQL 条件。"""

    conditions = []
    for scope_type, department_ids in authorization.permission_scopes("dashboard:view"):
        if scope_type == "all":
            return true()
        if scope_type == "self":
            if owner_user_id_column is not None:
                conditions.append(owner_user_id_column == authorization.user.id)
            elif owner_uid_column is not None:
                conditions.append(owner_uid_column == authorization.user.uid)
        elif scope_type == "organization_and_descendants" and authorization.user.department_id is not None:
            conditions.append(path_column.like(f"%/{authorization.user.department_id}/%"))
        elif scope_type == "selected_organizations_and_descendants":
            conditions.extend(path_column.like(f"%/{department_id}/%") for department_id in department_ids)
    return or_(*conditions) if conditions else false()


async def _dashboard_history_filter(
    db: AsyncSession,
    authorization: AuthorizationContext,
    path_column,
    department_id: int | None,
    *,
    owner_user_id_column=None,
    owner_uid_column=None,
):
    """生成历史事件可见条件，并隐藏越出当前管理域的筛选目标。"""

    if department_id is not None and not await department_is_accessible(
        authorization,
        "dashboard:view",
        department_id,
        db=db,
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="组织节点不存在")

    visibility = _historical_visibility_filter(
        authorization,
        path_column,
        owner_user_id_column=owner_user_id_column,
        owner_uid_column=owner_uid_column,
    )
    if department_id is None:
        return visibility
    return and_(visibility, path_column.like(f"%/{department_id}/%"))


async def _dashboard_resource_subjects(
    db: AsyncSession,
    authorization: AuthorizationContext,
    department_id: int | None,
) -> list[dict[str, Any]]:
    """生成当前 Dashboard 管理域内的用户和组织授权主体。"""

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
    selected_path = paths.get(department_id) if department_id is not None else None
    subjects = [{"uid": user.uid, "department_ancestor_ids": user.department_ancestor_ids} for user, _ in user_rows]
    subjects.extend(
        {
            "uid": "",
            "department_ancestor_ids": parse_department_ancestor_ids(paths.get(item["id"])),
        }
        for item in departments
        if authorization.allows(
            "dashboard:view",
            AuthorizationTarget(department_ancestor_ids=parse_department_ancestor_ids(paths.get(item["id"]))),
        )
        and (selected_path is None or paths[item["id"]].startswith(selected_path))
    )
    return subjects


def _get_time_group_format(column, time_range: str) -> Any:
    """
    根据数据库类型生成时间分组格式化表达式。
    PostgreSQL 使用 to_char + INTERVAL，SQLite 使用 datetime + strftime。
    """
    # 检查是否是 PostgreSQL（通过检测 engine 或使用方言）
    # 这里直接使用 PostgreSQL 语法，因为所有业务数据现在都在 PostgreSQL 上
    if time_range == "14hours":
        # 每小时: YYYY-MM-DD HH:00
        time_expr = func.to_char(column + text("INTERVAL '8 hours'"), "YYYY-MM-DD HH24:00")
    elif time_range == "14weeks":
        # 每周: YYYY-WW
        time_expr = func.to_char(column + text("INTERVAL '8 hours'"), "YYYY-IW")
    else:  # 14days
        # 每天: YYYY-MM-DD
        time_expr = func.to_char(column + text("INTERVAL '8 hours'"), "YYYY-MM-DD")
    return time_expr


# =============================================================================
# Response Models
# =============================================================================


class UserActivityStats(BaseModel):
    """用户活跃度统计"""

    total_users: int
    active_users_24h: int
    active_users_30d: int
    daily_active_users: list[dict]  # 最近7天每日活跃用户


class ToolCallStats(BaseModel):
    """工具调用统计"""

    total_calls: int
    successful_calls: int
    failed_calls: int
    success_rate: float
    most_used_tools: list[dict]
    tool_error_distribution: dict
    daily_tool_calls: list[dict]  # 最近7天每日工具调用数


class KnowledgeStats(BaseModel):
    """知识库统计"""

    total_databases: int
    total_files: int
    total_nodes: int
    total_storage_size: int  # 字节
    databases_by_type: dict
    file_type_distribution: dict


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
    """AI智能体分析"""

    total_agents: int
    agent_conversation_counts: list[dict]
    agent_satisfaction_rates: list[dict]
    agent_tool_usage: list[dict]
    top_performing_agents: list[dict]
    agent_names: dict[str, str] = {}  # agent_id -> agent_name 映射


class ConversationListItem(BaseModel):
    """Conversation list item"""

    thread_id: str
    uid: str
    agent_id: str
    title: str
    status: str
    message_count: int
    created_at: str
    updated_at: str


class ConversationDetailResponse(BaseModel):
    """Conversation detail"""

    thread_id: str
    uid: str
    agent_id: str
    title: str
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


@dashboard.get("/stats/current-organization", response_model=CurrentOrganizationStats)
async def get_current_organization_stats(
    department_id: int | None = None,
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
    db: AsyncSession = Depends(get_db),
):
    """按 Dashboard 查看者的数据范围统计当前组织子树。"""

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
    options = []
    for item in departments:
        selectable = authorization.allows(
            "dashboard:view",
            AuthorizationTarget(department_ancestor_ids=parse_department_ancestor_ids(paths.get(item["id"]))),
        )
        options.append({**item, "selectable": selectable})

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


# =============================================================================
# Conversation Management - 对话管理
# =============================================================================


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
    from yuxi.storage.postgres.models_business import Conversation, ConversationStats

    try:
        # Build query
        query = select(Conversation, ConversationStats).outerjoin(
            ConversationStats, Conversation.id == ConversationStats.conversation_id
        )
        query = query.filter(
            await _dashboard_history_filter(
                db,
                authorization,
                Conversation.organization_path_snapshot,
                department_id,
                owner_uid_column=Conversation.uid,
            )
        )

        # Apply filters
        if uid:
            query = query.filter(Conversation.uid == uid)
        if agent_id:
            query = query.filter(Conversation.agent_id == agent_id)
        if status != "all":
            query = query.filter(Conversation.status == status)

        # Order and paginate
        query = query.order_by(Conversation.updated_at.desc()).limit(limit).offset(offset)

        result = await db.execute(query)
        results = result.all()

        return [
            {
                "thread_id": conv.thread_id,
                "uid": conv.uid,
                "agent_id": conv.agent_id,
                "title": conv.title,
                "status": conv.status,
                "message_count": stats.message_count if stats else 0,
                "created_at": conv.created_at.isoformat(),
                "updated_at": conv.updated_at.isoformat(),
            }
            for conv, stats in results
        ]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting conversations: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get conversations: {str(e)}")


@dashboard.get("/conversations/{thread_id}", response_model=ConversationDetailResponse)
async def get_conversation_detail(
    thread_id: str,
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """获取管理域内的指定对话详情。"""
    try:
        conv_manager = ConversationRepository(db)
        conversation = await conv_manager.get_conversation_by_thread_id(thread_id)

        if not conversation:
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

        # Get messages and stats
        messages = await conv_manager.get_messages(conversation.id)
        stats = await conv_manager.get_stats(conversation.id)

        # Format messages
        message_list = []
        for msg in messages:
            msg_dict = {
                "id": msg.id,
                "role": msg.role,
                "content": msg.content,
                "message_type": msg.message_type,
                "created_at": msg.created_at.isoformat(),
            }

            # Include tool calls if present
            if msg.tool_calls:
                msg_dict["tool_calls"] = [
                    {
                        "id": tc.id,
                        "tool_name": tc.tool_name,
                        "tool_input": tc.tool_input,
                        "tool_output": tc.tool_output,
                        "status": tc.status,
                    }
                    for tc in msg.tool_calls
                ]

            message_list.append(msg_dict)

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
    except Exception as e:
        logger.error(f"Error getting conversation detail: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get conversation detail: {str(e)}")


# =============================================================================
# 用户活动统计（超级管理员权限）
# =============================================================================


@dashboard.get("/stats/users", response_model=UserActivityStats)
async def get_user_activity_stats(
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按历史组织快照获取用户活动统计。"""
    try:
        from yuxi.storage.postgres.models_business import Conversation, User

        now = utc_now()
        # PostgreSQL with asyncpg requires naive datetime for naive DateTime columns
        naive_now = now.replace(tzinfo=None)

        # Conversations may store either the numeric user primary key or the login uid string.
        # Join condition accounts for both representations.
        user_join_condition = Conversation.uid == User.uid
        history_filter = await _dashboard_history_filter(
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
        total_users = len(current_users)

        # 不同时间段的活跃用户数（基于对话活动，排除已删除用户）
        active_users_24h_result = await db.execute(
            select(func.count(distinct(User.id)))
            .select_from(Conversation)
            .join(User, user_join_condition)
            .filter(Conversation.updated_at >= naive_now - timedelta(days=1), User.is_deleted == 0, history_filter)
        )
        active_users_24h = active_users_24h_result.scalar() or 0

        active_users_30d_result = await db.execute(
            select(func.count(distinct(User.id)))
            .select_from(Conversation)
            .join(User, user_join_condition)
            .filter(Conversation.updated_at >= naive_now - timedelta(days=30), User.is_deleted == 0, history_filter)
        )
        active_users_30d = active_users_30d_result.scalar() or 0
        # 最近7天每日活跃用户（排除已删除用户）
        daily_active_users = []
        for i in range(7):
            day_start = naive_now - timedelta(days=i + 1)
            day_end = naive_now - timedelta(days=i)

            active_count_result = await db.execute(
                select(func.count(distinct(User.id)))
                .select_from(Conversation)
                .join(User, user_join_condition)
                .filter(
                    Conversation.updated_at >= day_start,
                    Conversation.updated_at < day_end,
                    User.is_deleted == 0,
                    history_filter,
                )
            )
            active_count = active_count_result.scalar() or 0

            daily_active_users.append({"date": day_start.strftime("%Y-%m-%d"), "active_users": active_count})

        return UserActivityStats(
            total_users=total_users,
            active_users_24h=active_users_24h,
            active_users_30d=active_users_30d,
            daily_active_users=list(reversed(daily_active_users)),  # 按时间正序
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting user activity stats: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get user activity stats: {str(e)}")


# =============================================================================
# Tool Call Statistics - 工具调用统计
# =============================================================================


@dashboard.get("/stats/tools", response_model=ToolCallStats)
async def get_tool_call_stats(
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按历史组织快照获取工具调用统计。"""
    try:
        from yuxi.storage.postgres.models_business import ToolCall

        now = utc_now()
        # PostgreSQL with asyncpg requires naive datetime for naive DateTime columns
        naive_now = now.replace(tzinfo=None)
        history_filter = await _dashboard_history_filter(
            db,
            authorization,
            ToolCall.organization_path_snapshot,
            department_id,
        )

        # 基础工具调用统计
        total_calls_result = await db.execute(select(func.count(ToolCall.id)).filter(history_filter))
        total_calls = total_calls_result.scalar() or 0

        successful_calls_result = await db.execute(
            select(func.count(ToolCall.id)).filter(ToolCall.status == "success", history_filter)
        )
        successful_calls = successful_calls_result.scalar() or 0
        failed_calls = total_calls - successful_calls
        success_rate = round((successful_calls / total_calls * 100), 2) if total_calls > 0 else 0

        # 最常用工具
        most_used_tools_result = await db.execute(
            select(ToolCall.tool_name, func.count(ToolCall.id).label("count"))
            .filter(history_filter)
            .group_by(ToolCall.tool_name)
            .order_by(func.count(ToolCall.id).desc())
            .limit(10)
        )
        most_used_tools = most_used_tools_result.all()
        most_used_tools = [{"tool_name": name, "count": count} for name, count in most_used_tools]

        # 工具错误分布
        tool_errors_result = await db.execute(
            select(ToolCall.tool_name, func.count(ToolCall.id).label("error_count"))
            .filter(ToolCall.status == "error", history_filter)
            .group_by(ToolCall.tool_name)
        )
        tool_errors = tool_errors_result.all()
        tool_error_distribution = {name: count for name, count in tool_errors}

        # 最近7天每日工具调用数
        daily_tool_calls = []
        for i in range(7):
            day_start = naive_now - timedelta(days=i + 1)
            day_end = naive_now - timedelta(days=i)

            daily_count_result = await db.execute(
                select(func.count(ToolCall.id)).filter(
                    ToolCall.created_at >= day_start,
                    ToolCall.created_at < day_end,
                    history_filter,
                )
            )
            daily_count = daily_count_result.scalar() or 0

            daily_tool_calls.append({"date": day_start.strftime("%Y-%m-%d"), "call_count": daily_count})

        return ToolCallStats(
            total_calls=total_calls,
            successful_calls=successful_calls,
            failed_calls=failed_calls,
            success_rate=success_rate,
            most_used_tools=most_used_tools,
            tool_error_distribution=tool_error_distribution,
            daily_tool_calls=list(reversed(daily_tool_calls)),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting tool call stats: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get tool call stats: {str(e)}")


# =============================================================================
# 资源创建归属与共享可见统计
# =============================================================================


@dashboard.get("/stats/resources", response_model=ResourceScopeStats)
async def get_resource_scope_stats(
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """分别统计资源创建组织和当前共享可见范围。"""

    subjects = await _dashboard_resource_subjects(db, authorization, department_id)

    async def summarize(model, resolver) -> ResourceScopeMetric:
        """按资源主键去重后汇总一种资源。"""

        resources = list(await db.scalars(select(model)))
        created_resources = []
        shared_resources = []
        # ponytail: 直接复用现有 ACL；资源或组织主体量出现慢查询后再下推为 SQL。
        for resource in resources:
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

    knowledge_bases = await summarize(KnowledgeBase, resolve_knowledge_base_permission)
    agents = await summarize(Agent, resolve_agent_permission)
    skills = await summarize(Skill, resolve_skill_permission)
    return ResourceScopeStats(
        knowledge_bases=knowledge_bases,
        agents=agents,
        skills=skills,
        contains_inferred_data=any(metric.contains_inferred_data for metric in (knowledge_bases, agents, skills)),
    )


# =============================================================================
# 知识库共享可见统计
# =============================================================================


@dashboard.get("/stats/knowledge", response_model=KnowledgeStats)
async def get_knowledge_stats(
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按当前管理域内的共享可见知识库统计文件和容量。"""
    try:
        from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository
        from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

        kb_repo = KnowledgeBaseRepository()
        file_repo = KnowledgeFileRepository()

        subjects = await _dashboard_resource_subjects(db, authorization, department_id)
        kb_rows = [
            kb
            for kb in await kb_repo.get_all()
            if any(resolve_knowledge_base_permission(subject, kb) != ResourcePermission.NONE for subject in subjects)
        ]
        total_databases = len(kb_rows)

        databases_by_type: dict[str, int] = {}
        files_by_type: dict[str, int] = {}
        total_files = 0
        total_nodes = 0
        total_storage_size = 0

        file_type_mapping = {
            "txt": "文本文件",
            "pdf": "PDF文档",
            "docx": "Word文档",
            "doc": "Word文档",
            "md": "Markdown",
            "html": "HTML网页",
            "htm": "HTML网页",
            "json": "JSON数据",
            "csv": "CSV表格",
            "xlsx": "Excel表格",
            "xls": "Excel表格",
            "pptx": "PowerPoint",
            "ppt": "PowerPoint",
            "png": "PNG图片",
            "jpg": "JPEG图片",
            "jpeg": "JPEG图片",
            "gif": "GIF图片",
            "svg": "SVG图片",
            "mp4": "MP4视频",
            "mp3": "MP3音频",
            "zip": "ZIP压缩包",
            "rar": "RAR压缩包",
            "7z": "7Z压缩包",
        }

        for kb in kb_rows:
            kb_type = (kb.kb_type or "unknown").lower()
            display_type = {
                "faiss": "FAISS",
                "milvus": "Milvus",
                "dify": "Dify",
                "qdrant": "Qdrant",
                "elasticsearch": "Elasticsearch",
                "unknown": "未知类型",
            }.get(kb_type, kb.kb_type or "未知类型")
            databases_by_type[display_type] = databases_by_type.get(display_type, 0) + 1

            files = await file_repo.list_by_kb_id(kb.kb_id)
            total_files += len(files)
            for record in files:
                file_ext = (record.file_type or "").lower()
                display_name = file_type_mapping.get(file_ext, file_ext.upper() + "文件" if file_ext else "其他")
                files_by_type[display_name] = files_by_type.get(display_name, 0) + 1
                total_storage_size += int(record.file_size or 0)

        return KnowledgeStats(
            total_databases=total_databases,
            total_files=total_files,
            total_nodes=total_nodes,
            total_storage_size=total_storage_size,
            databases_by_type=databases_by_type,
            file_type_distribution=files_by_type,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting knowledge stats: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get knowledge stats: {str(e)}")


# =============================================================================
# 智能体分析（超级管理员权限）
# =============================================================================


@dashboard.get("/stats/agents", response_model=AgentAnalytics)
async def get_agent_analytics(
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按历史组织快照获取智能体分析。"""
    try:
        from yuxi.storage.postgres.models_business import Conversation, Message, MessageFeedback, ToolCall

        conversation_filter = await _dashboard_history_filter(
            db,
            authorization,
            Conversation.organization_path_snapshot,
            department_id,
            owner_uid_column=Conversation.uid,
        )
        feedback_filter = await _dashboard_history_filter(
            db,
            authorization,
            MessageFeedback.organization_path_snapshot,
            department_id,
            owner_uid_column=MessageFeedback.uid,
        )
        tool_filter = await _dashboard_history_filter(
            db,
            authorization,
            ToolCall.organization_path_snapshot,
            department_id,
        )

        # 获取所有智能体
        agents_result = await db.execute(
            select(Conversation.agent_id, func.count(Conversation.id).label("conversation_count"))
            .filter(conversation_filter)
            .group_by(Conversation.agent_id)
        )
        agents = agents_result.all()

        total_agents = len(agents)
        agent_conversation_counts = [{"agent_id": agent_id, "conversation_count": count} for agent_id, count in agents]

        # 智能体满意度统计
        agent_satisfaction = []
        for agent_id, _ in agents:
            total_feedbacks_result = await db.execute(
                select(func.count(MessageFeedback.id))
                .join(Message, MessageFeedback.message_id == Message.id)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .filter(Conversation.agent_id == agent_id, feedback_filter)
            )
            total_feedbacks = total_feedbacks_result.scalar() or 0

            positive_feedbacks_result = await db.execute(
                select(func.count(MessageFeedback.id))
                .join(Message, MessageFeedback.message_id == Message.id)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .filter(Conversation.agent_id == agent_id, MessageFeedback.rating == "like", feedback_filter)
            )
            positive_feedbacks = positive_feedbacks_result.scalar() or 0

            satisfaction_rate = round((positive_feedbacks / total_feedbacks * 100), 2) if total_feedbacks > 0 else 100

            agent_satisfaction.append(
                {"agent_id": agent_id, "satisfaction_rate": satisfaction_rate, "total_feedbacks": total_feedbacks}
            )

        # 智能体工具使用统计
        agent_tool_usage = []
        for agent_id, _ in agents:
            tool_usage_count_result = await db.execute(
                select(func.count(ToolCall.id))
                .join(Message, ToolCall.message_id == Message.id)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .filter(Conversation.agent_id == agent_id, tool_filter)
            )
            tool_usage_count = tool_usage_count_result.scalar() or 0

            agent_tool_usage.append({"agent_id": agent_id, "tool_usage_count": tool_usage_count})

        # 表现最佳的智能体（按对话数排序）
        top_performing_agents = []
        for i, (agent_id, conv_count) in enumerate(agents):
            # 获取满意度数据
            satisfaction_data = next(
                (s for s in agent_satisfaction if s["agent_id"] == agent_id), {"satisfaction_rate": 0}
            )

            top_performing_agents.append(
                {
                    "agent_id": agent_id,
                    "conversation_count": conv_count,
                    "satisfaction_rate": satisfaction_data["satisfaction_rate"],
                }
            )

        # 按对话数排序，取前5名
        top_performing_agents.sort(key=lambda x: x["conversation_count"], reverse=True)
        top_performing_agents = top_performing_agents[:5]

        agent_slugs = [agent_id for agent_id, _ in agents if agent_id]
        agent_names = {}
        if agent_slugs:
            agent_repo = AgentRepository(db)
            agent_names = {agent.slug: agent.name for agent in await agent_repo.list_by_slugs(agent_slugs)}

        return AgentAnalytics(
            total_agents=total_agents,
            agent_conversation_counts=agent_conversation_counts,
            agent_satisfaction_rates=agent_satisfaction,
            agent_tool_usage=agent_tool_usage,
            top_performing_agents=top_performing_agents,
            agent_names=agent_names,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting agent analytics: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get agent analytics: {str(e)}")


# =============================================================================
# 基础统计（超级管理员权限）
# =============================================================================


@dashboard.get("/stats")
async def get_dashboard_stats(
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按当前人员关系和历史事件快照获取基础统计。"""
    from yuxi.storage.postgres.models_business import (
        Conversation,
        Message,
        MessageFeedback,
        OperationLog,
        SecurityAudit,
        ToolCall,
    )

    try:
        conversation_filter = await _dashboard_history_filter(
            db,
            authorization,
            Conversation.organization_path_snapshot,
            department_id,
            owner_uid_column=Conversation.uid,
        )
        feedback_filter = await _dashboard_history_filter(
            db,
            authorization,
            MessageFeedback.organization_path_snapshot,
            department_id,
            owner_uid_column=MessageFeedback.uid,
        )
        tool_filter = await _dashboard_history_filter(
            db,
            authorization,
            ToolCall.organization_path_snapshot,
            department_id,
        )
        operation_filter = await _dashboard_history_filter(
            db,
            authorization,
            OperationLog.organization_path_snapshot,
            department_id,
            owner_user_id_column=OperationLog.user_id,
        )
        audit_filter = await _dashboard_history_filter(
            db,
            authorization,
            SecurityAudit.organization_path_snapshot,
            department_id,
            owner_user_id_column=SecurityAudit.actor_user_id,
        )

        # Basic counts
        total_conversations_result = await db.execute(select(func.count(Conversation.id)).filter(conversation_filter))
        total_conversations = total_conversations_result.scalar() or 0

        active_conversations_result = await db.execute(
            select(func.count(Conversation.id)).filter(Conversation.status == "active", conversation_filter)
        )
        active_conversations = active_conversations_result.scalar() or 0

        total_messages_result = await db.execute(
            select(func.count(Message.id))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .filter(conversation_filter)
        )
        total_messages = total_messages_result.scalar() or 0

        current_users = await list_authorized_users(
            authorization,
            "dashboard:view",
            department_id=department_id,
            db=db,
        )
        if current_users is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="组织节点不存在")
        total_users = len(current_users)

        # Feedback statistics
        total_feedbacks_result = await db.execute(select(func.count(MessageFeedback.id)).filter(feedback_filter))
        total_feedbacks = total_feedbacks_result.scalar() or 0

        like_count_result = await db.execute(
            select(func.count(MessageFeedback.id)).filter(MessageFeedback.rating == "like", feedback_filter)
        )
        like_count = like_count_result.scalar() or 0

        # Calculate satisfaction rate
        satisfaction_rate = round((like_count / total_feedbacks * 100), 2) if total_feedbacks > 0 else 100
        inferred_checks = (
            (Conversation, conversation_filter),
            (ToolCall, tool_filter),
            (MessageFeedback, feedback_filter),
            (OperationLog, operation_filter),
            (SecurityAudit, audit_filter),
        )
        contains_inferred_data = False
        for model, scope_filter in inferred_checks:
            inferred_id = await db.scalar(
                select(model.id).filter(scope_filter, model.organization_snapshot_inferred.is_(True)).limit(1)
            )
            if inferred_id is not None:
                contains_inferred_data = True
                break

        return {
            "total_conversations": total_conversations,
            "active_conversations": active_conversations,
            "total_messages": total_messages,
            "total_users": total_users,
            "contains_inferred_data": contains_inferred_data,
            "feedback_stats": {
                "total_feedbacks": total_feedbacks,
                "satisfaction_rate": satisfaction_rate,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting dashboard stats: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get dashboard stats: {str(e)}")


# =============================================================================
# 反馈管理（超级管理员权限）
# =============================================================================


class FeedbackListItem(BaseModel):
    """反馈列表项"""

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


@dashboard.get("/feedbacks", response_model=list[FeedbackListItem])
async def get_all_feedbacks(
    rating: str | None = None,
    agent_id: str | None = None,
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按历史组织快照获取反馈记录。"""
    from yuxi.storage.postgres.models_business import Conversation, Message, MessageFeedback, User

    try:
        query = (
            select(MessageFeedback, Message, Conversation, User)
            .join(Message, MessageFeedback.message_id == Message.id)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .outerjoin(User, MessageFeedback.uid == User.uid)
        )
        query = query.filter(
            await _dashboard_history_filter(
                db,
                authorization,
                MessageFeedback.organization_path_snapshot,
                department_id,
                owner_uid_column=MessageFeedback.uid,
            )
        )

        # Apply filters
        if rating and rating in ["like", "dislike"]:
            query = query.filter(MessageFeedback.rating == rating)
        if agent_id:
            query = query.filter(Conversation.agent_id == agent_id)

        # Order by creation time (most recent first)
        query = query.order_by(MessageFeedback.created_at.desc())

        results = await db.execute(query)
        results = results.all()

        # Debug logging (privacy-safe)
        logger.info(f"Found {len(results)} feedback records")
        # Removed sensitive user data from logs for privacy compliance

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
    except Exception as e:
        logger.error(f"Error getting feedbacks: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get feedbacks: {str(e)}")


# =============================================================================
# 调用分析时间序列统计（超级管理员权限）
# =============================================================================


class TimeSeriesStats(BaseModel):
    """时间序列统计数据"""

    data: list[dict]  # [{"date": "2024-01-01", "data": {"item1": 50, "item2": 30}, "total": 80}, ...]
    categories: list[str]  # 所有类别名称
    total_count: int
    average_count: float
    peak_count: int
    peak_date: str
    agent_names: dict[str, str] | None = None  # agent_id -> agent_name 映射（仅 type=agents）


@dashboard.get("/stats/calls/timeseries", response_model=TimeSeriesStats)
async def get_call_timeseries_stats(
    type: str = "models",  # models/agents/tokens/tools
    time_range: str = "14days",  # 14hours/14days/14weeks
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按历史组织快照获取调用分析时间序列。"""
    try:
        from yuxi.storage.postgres.models_business import Conversation, Message, ToolCall

        conversation_filter = await _dashboard_history_filter(
            db,
            authorization,
            Conversation.organization_path_snapshot,
            department_id,
            owner_uid_column=Conversation.uid,
        )
        tool_filter = await _dashboard_history_filter(
            db,
            authorization,
            ToolCall.organization_path_snapshot,
            department_id,
        )

        # 计算时间范围（使用北京时间 UTC+8）
        now = utc_now()
        local_now = shanghai_now()

        if time_range == "14hours":
            intervals = 14
            # 包含当前小时：从13小时前开始
            start_time = now - timedelta(hours=intervals - 1)
            group_format = _get_time_group_format(Message.created_at, time_range)
            base_local_time = ensure_shanghai(start_time)
        elif time_range == "14weeks":
            intervals = 14
            # 包含当前周：从13周前开始，并对齐到当周周一 00:00
            local_start = local_now - timedelta(weeks=intervals - 1)
            local_start = local_start - timedelta(days=local_start.weekday())
            local_start = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
            start_time = local_start.astimezone(UTC)
            group_format = _get_time_group_format(Message.created_at, time_range)
            base_local_time = local_start
        else:  # 14days (default)
            intervals = 14
            # 包含当前天：从13天前开始
            start_time = now - timedelta(days=intervals - 1)
            group_format = _get_time_group_format(Message.created_at, time_range)
            base_local_time = ensure_shanghai(start_time)

        # Convert start_time to naive UTC datetime for PostgreSQL query
        # PostgreSQL with asyncpg and naive DateTime columns requires naive datetime objects
        query_start_time = start_time.replace(tzinfo=None)

        # 根据类型查询数据
        if type == "models":
            # 模型调用统计（基于消息数量，按模型分组）
            # 从message的extra_metadata中提取模型信息
            category_expr = cast(Message.extra_metadata["response_metadata"]["model_name"], String)
            query_result = await db.execute(
                select(
                    group_format.label("date"),
                    func.count(Message.id).label("count"),
                    category_expr.label("category"),
                )
                .join(Conversation, Message.conversation_id == Conversation.id)
                .filter(Message.role == "assistant", Message.created_at >= query_start_time)
                .filter(Message.extra_metadata.isnot(None), conversation_filter)
                .group_by(group_format, category_expr)
                .order_by(group_format)
            )
            query = query_result.all()
        elif type == "agents":
            # 智能体调用统计（基于对话更新时间，按智能体分组）
            # 为对话创建独立的时间格式化器（使用 PostgreSQL 兼容的 to_char + INTERVAL）
            conv_group_format = _get_time_group_format(Conversation.updated_at, time_range)

            query_result = await db.execute(
                select(
                    conv_group_format.label("date"),
                    func.count(Conversation.id).label("count"),
                    Conversation.agent_id.label("category"),
                )
                .filter(Conversation.updated_at.isnot(None), conversation_filter)
                .filter(Conversation.updated_at >= query_start_time)
                .group_by(conv_group_format, Conversation.agent_id)
                .order_by(conv_group_format)
            )
            query = query_result.all()
        elif type == "tokens":
            # Token消耗统计（区分input/output tokens）
            # 先查询input tokens
            from sqlalchemy import literal

            input_query_result = await db.execute(
                select(
                    group_format.label("date"),
                    func.sum(
                        func.coalesce(
                            cast(cast(Message.extra_metadata["usage_metadata"]["input_tokens"], String), Integer), 0
                        )
                    ).label("count"),
                    literal("input_tokens").label("category"),
                )
                .join(Conversation, Message.conversation_id == Conversation.id)
                .filter(
                    Message.created_at >= query_start_time,
                    Message.extra_metadata.isnot(None),
                    Message.extra_metadata["usage_metadata"].isnot(None),
                    conversation_filter,
                )
                .group_by(group_format)
                .order_by(group_format)
            )
            input_query = input_query_result.all()

            # 查询output tokens
            output_query_result = await db.execute(
                select(
                    group_format.label("date"),
                    func.sum(
                        func.coalesce(
                            cast(cast(Message.extra_metadata["usage_metadata"]["output_tokens"], String), Integer), 0
                        )
                    ).label("count"),
                    literal("output_tokens").label("category"),
                )
                .join(Conversation, Message.conversation_id == Conversation.id)
                .filter(
                    Message.created_at >= query_start_time,
                    Message.extra_metadata.isnot(None),
                    Message.extra_metadata["usage_metadata"].isnot(None),
                    conversation_filter,
                )
                .group_by(group_format)
                .order_by(group_format)
            )
            output_query = output_query_result.all()

            # 合并两个查询结果
            input_results = input_query
            output_results = output_query
            results = input_results + output_results
        elif type == "tools":
            # 工具调用统计（按工具名称分组）
            # 为工具调用创建独立的时间格式化器（使用 PostgreSQL 兼容的 to_char + INTERVAL）
            tool_group_format = _get_time_group_format(ToolCall.created_at, time_range)

            query_result = await db.execute(
                select(
                    tool_group_format.label("date"),
                    func.count(ToolCall.id).label("count"),
                    ToolCall.tool_name.label("category"),
                )
                .filter(ToolCall.created_at >= query_start_time, tool_filter)
                .group_by(tool_group_format, ToolCall.tool_name)
                .order_by(tool_group_format)
            )
            query = query_result.all()
        else:
            raise HTTPException(status_code=422, detail=f"Invalid type: {type}")

        if type != "tokens":
            results = query

        # 处理堆叠数据格式
        # 首先收集所有类别
        categories = set()
        for result in results:
            if hasattr(result, "category") and result.category:
                categories.add(result.category)

        # 如果没有类别数据，提供默认类别
        if not categories:
            if type == "models":
                categories.add("unknown_model")
            elif type == "agents":
                categories.add("unknown_agent")
            elif type == "tokens":
                categories.update(["input_tokens", "output_tokens"])
            elif type == "tools":
                categories.add("unknown_tool")

        categories = sorted(list(categories))

        agent_names = None
        if type == "agents" and categories:
            agent_slugs = [c for c in categories if c]
            if agent_slugs:
                agent_repo = AgentRepository(db)
                agent_names = {agent.slug: agent.name for agent in await agent_repo.list_by_slugs(agent_slugs)}

        # 重新组织数据：按时间点分组每个类别的数据
        time_data = {}

        def normalize_week_key(raw_key: str) -> str:
            base_date = datetime.strptime(f"{raw_key}-1", "%Y-%W-%w")
            iso_year, iso_week, _ = base_date.isocalendar()
            return f"{iso_year}-{iso_week:02d}"

        for result in results:
            date_key = result.date
            if time_range == "14weeks":
                date_key = normalize_week_key(date_key)
            category = getattr(result, "category", "unknown")
            count = result.count

            if date_key not in time_data:
                time_data[date_key] = {}

            time_data[date_key][category] = count

        # 填充缺失的时间点（使用北京时间）
        data = []
        # 从起始点开始（北京时间）
        current_time = base_local_time

        if time_range == "14hours":
            delta = timedelta(hours=1)
        elif time_range == "14weeks":
            delta = timedelta(weeks=1)
        else:
            delta = timedelta(days=1)

        for i in range(intervals):
            if time_range == "14hours":
                date_key = current_time.strftime("%Y-%m-%d %H:00")
            elif time_range == "14weeks":
                iso_year, iso_week, _ = current_time.isocalendar()
                date_key = f"{iso_year}-{iso_week:02d}"
            else:
                date_key = current_time.strftime("%Y-%m-%d")

            # 获取该时间点的数据
            day_data = time_data.get(date_key, {})
            day_total = sum(day_data.values())

            # 确保所有类别都有值（缺失的补0）
            for category in categories:
                if category not in day_data:
                    day_data[category] = 0

            data.append({"date": date_key, "data": day_data, "total": day_total})
            current_time += delta

        # 计算统计指标
        if type == "tools":
            # 对于工具调用，显示所有时间的总数（与ToolStatsComponent保持一致）
            from yuxi.storage.postgres.models_business import ToolCall

            total_count_result = await db.execute(select(func.count(ToolCall.id)).filter(tool_filter))
            total_count = total_count_result.scalar() or 0
        else:
            # 其他类型使用时间序列数据的总和
            total_count = sum(item["total"] for item in data)

        average_count = round(total_count / intervals, 2) if intervals > 0 else 0
        peak_data = max(data, key=lambda x: x["total"]) if data else {"total": 0, "date": ""}

        return TimeSeriesStats(
            data=data,
            categories=categories,
            total_count=total_count,
            average_count=average_count,
            peak_count=peak_data["total"],
            peak_date=peak_data["date"],
            agent_names=agent_names,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting call timeseries stats: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get call timeseries stats: {str(e)}")
