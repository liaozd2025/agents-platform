"""角色与权限管理接口。"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.services.role_service import (
    RoleConflictError,
    RoleNotFoundError,
    copy_role,
    create_custom_role,
    deactivate_custom_role,
    get_role_overview,
    update_custom_role,
)
from yuxi.services.user_role_service import has_active_role
from yuxi.permissions.authorization import AuthorizationContext
from yuxi.services.user_management_service import get_authorized_user
from server.utils.auth_middleware import get_authorization_context, get_db, require_permission

roles = APIRouter(prefix="/roles", tags=["roles"])


class RoleDefinitionRequest(BaseModel):
    """创建自定义角色的完整定义。"""

    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    permission_keys: list[str] = Field(default_factory=list)
    default_scope_type: str = Field(min_length=1, max_length=64)
    default_department_ids: list[int] = Field(default_factory=list)


class RoleUpdateRequest(BaseModel):
    """更新自定义角色的完整定义。"""

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    permission_keys: list[str] = Field(default_factory=list)
    default_scope_type: str = Field(min_length=1, max_length=64)
    default_department_ids: list[int] = Field(default_factory=list)


class RoleCopyRequest(BaseModel):
    """复制角色时需要提供的新标识和名称。"""

    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)


def _raise_role_error(error: ValueError) -> None:
    """把角色用例错误映射为稳定 HTTP 状态码。"""

    if isinstance(error, RoleNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    if isinstance(error, RoleConflictError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


def require_role_definition_manager(
    authorization: AuthorizationContext = Depends(require_permission("role:manage")),
) -> AuthorizationContext:
    """仅允许新角色模型中的超级管理员维护角色定义。"""

    if not has_active_role(authorization.user, "superadmin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有超级管理员可以维护角色定义")
    return authorization


@roles.get("/overview")
async def read_role_overview(
    target_user_id: int | None = None,
    authorization: AuthorizationContext = Depends(get_authorization_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """返回当前用户有权查看的角色、权限、范围、成员和审计。"""

    if target_user_id is None:
        if not authorization.has_permission("role:read"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="缺少功能权限: role:read")
        return await get_role_overview(db, authorization)

    if not authorization.has_permission("user:role_assign"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="缺少功能权限: user:role_assign")
    target = await get_authorized_user(db, authorization, "user:role_assign", target_user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    return await get_role_overview(db, authorization, assignment_target=target)


@roles.post("", status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: RoleDefinitionRequest,
    authorization: AuthorizationContext = Depends(require_role_definition_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """创建自定义角色。"""

    try:
        return await create_custom_role(db, authorization.user, **payload.model_dump())
    except ValueError as error:
        _raise_role_error(error)


@roles.post("/{role_id}/copy", status_code=status.HTTP_201_CREATED)
async def copy_existing_role(
    role_id: int,
    payload: RoleCopyRequest,
    authorization: AuthorizationContext = Depends(require_role_definition_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """复制已有角色为独立自定义角色。"""

    try:
        return await copy_role(db, authorization.user, role_id, **payload.model_dump())
    except ValueError as error:
        _raise_role_error(error)


@roles.put("/{role_id}")
async def update_role(
    role_id: int,
    payload: RoleUpdateRequest,
    authorization: AuthorizationContext = Depends(require_role_definition_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """修改自定义角色。"""

    try:
        return await update_custom_role(db, authorization.user, role_id, **payload.model_dump())
    except ValueError as error:
        _raise_role_error(error)


@roles.post("/{role_id}/deactivate")
async def deactivate_role(
    role_id: int,
    authorization: AuthorizationContext = Depends(require_role_definition_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """停用没有有效成员的自定义角色。"""

    try:
        return await deactivate_custom_role(db, authorization.user, role_id)
    except ValueError as error:
        _raise_role_error(error)
