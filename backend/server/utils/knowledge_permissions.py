"""将知识库领域权限校验适配为 FastAPI 依赖。"""

from fastapi import Depends, HTTPException, status

from server.utils.auth_middleware import get_authorization_context, require_permission
from yuxi.permissions.authorization import AuthorizationContext
from yuxi.knowledge.read_models import KnowledgeBaseDetail
from yuxi.knowledge.runtime import knowledge_base
from yuxi.permissions import (
    ResourcePermission,
    ResourcePermissionDenied,
    require_knowledge_base_permission,
)
from yuxi.storage.postgres.models_business import User


async def ensure_knowledge_base_permission(
    kb_id: str,
    current_user: User,
    required: ResourcePermission,
) -> KnowledgeBaseDetail:
    """加载知识库并校验当前用户的有效资源权限。"""

    db_info = await knowledge_base.get_database_info(kb_id)
    if not db_info:
        raise HTTPException(status_code=404, detail=f"知识库 {kb_id} 不存在")

    try:
        require_knowledge_base_permission(current_user, db_info, required)
    except ResourcePermissionDenied as error:
        raise HTTPException(status_code=404, detail=f"知识库 {kb_id} 不存在") from error
    return db_info


async def require_knowledge_base_read_permission(
    authorization: AuthorizationContext = Depends(get_authorization_context),
) -> User:
    """校验知识库读取或管理功能权限。"""

    if not any(
        authorization.has_permission(permission)
        for permission in ("knowledge_base:read", "knowledge_base:manage")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="缺少功能权限: knowledge_base:read",
        )

    return authorization.user


async def require_knowledge_base_manage_permission(
    authorization: AuthorizationContext = Depends(require_permission("knowledge_base:manage")),
) -> User:
    """校验知识库管理功能权限。"""

    return authorization.user


async def require_knowledge_base_read(
    kb_id: str,
    current_user: User = Depends(require_knowledge_base_read_permission),
) -> User:
    """校验知识库读取功能权限与具体资源共享范围。"""

    await ensure_knowledge_base_permission(kb_id, current_user, ResourcePermission.READ)
    return current_user


async def require_knowledge_base_manage(
    kb_id: str,
    current_user: User = Depends(require_knowledge_base_manage_permission),
) -> User:
    """校验知识库管理功能权限与具体资源共享范围。"""

    await ensure_knowledge_base_permission(kb_id, current_user, ResourcePermission.MANAGE)
    return current_user
