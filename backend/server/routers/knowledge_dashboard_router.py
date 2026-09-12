"""知识域 Dashboard HTTP 路由。"""

import traceback

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.permissions.authorization import AuthorizationContext
from yuxi.services.dashboard_scope_service import dashboard_resource_subjects
from yuxi.services.knowledge_dashboard_service import get_knowledge_stats
from yuxi.utils.logging_config import logger

from server.utils.auth_middleware import get_db, require_permission

knowledge_dashboard = APIRouter(prefix="/dashboard", tags=["Dashboard"])


class KnowledgeStats(BaseModel):
    """知识库统计。"""

    total_databases: int
    total_files: int
    total_nodes: int
    total_storage_size: int
    databases_by_type: dict
    file_type_distribution: dict


@knowledge_dashboard.get("/stats/knowledge", response_model=KnowledgeStats)
async def read_knowledge_stats(
    department_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: AuthorizationContext = Depends(require_permission("dashboard:view")),
):
    """按当前 Dashboard 管理域汇总共享可见知识库。"""

    try:
        subjects = await dashboard_resource_subjects(db, authorization, department_id)
        return KnowledgeStats(**await get_knowledge_stats(subjects))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error getting knowledge stats: {exc}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to get knowledge stats: {str(exc)}") from exc
