"""组织机构管理的 HTTP 请求与响应适配。"""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.api.dependencies.auth import get_authorization_context, get_db, require_permission
from yuxi.api.middleware.access_log import extract_client_ip
from yuxi.modules.identity.permissions import AuthorizationContext
from yuxi.modules.identity.services import departments as service

department = APIRouter(prefix="/departments", tags=["department"])


class DepartmentCreate(BaseModel):
    """创建组织节点请求"""

    name: str
    description: str | None = None
    parent_id: int | None = None  # 不传时挂在集团根下
    node_type: Literal["group", "company", "department"] = "department"
    # 可选的管理员信息
    admin_uid: str | None = None
    admin_password: str | None = Field(default=None, min_length=8)
    admin_phone: str | None = None


class DepartmentUpdate(BaseModel):
    """更新组织节点请求"""

    name: str | None = None
    description: str | None = None
    parent_id: int | None = None


class DepartmentResponse(BaseModel):
    """组织节点响应"""

    id: int
    name: str
    description: str | None = None
    created_at: str
    parent_id: int | None = None
    node_type: str
    user_count: int = 0


@department.get("", response_model=list[DepartmentResponse])
async def get_departments(authorization: AuthorizationContext = Depends(get_authorization_context)):
    """返回授权范围内的组织树与导航祖先。"""
    return await service.get_departments(authorization=authorization)


@department.get("/{department_id}", response_model=DepartmentResponse)
async def get_department(
    department_id: int, authorization: AuthorizationContext = Depends(get_authorization_context), db: AsyncSession = Depends(get_db)
):
    """读取指定组织节点。"""
    return await service.get_department(db, department_id=department_id, authorization=authorization)


@department.post("", response_model=DepartmentResponse, status_code=201)
async def create_department(
    data: DepartmentCreate,
    request: Request,
    authorization: AuthorizationContext = Depends(require_permission("department:create")),
    db: AsyncSession = Depends(get_db),
):
    """创建组织节点及可选管理员。"""
    return await service.create_department(
        db, authorization=authorization, ip_address=extract_client_ip(request) if request else None, **data.model_dump()
    )


@department.put("/{department_id}", response_model=DepartmentResponse)
async def update_department(
    department_id: int,
    data: DepartmentUpdate,
    request: Request,
    authorization: AuthorizationContext = Depends(require_permission("department:update")),
    db: AsyncSession = Depends(get_db),
):
    """更新组织属性或移动子树。"""
    return await service.update_department(
        db,
        department_id=department_id,
        authorization=authorization,
        ip_address=extract_client_ip(request) if request else None,
        parent_id_provided="parent_id" in data.model_fields_set,
        **data.model_dump(),
    )


@department.delete("/{department_id}")
async def delete_department(
    department_id: int,
    request: Request,
    authorization: AuthorizationContext = Depends(require_permission("department:delete")),
    db: AsyncSession = Depends(get_db),
):
    """删除满足保护条件的组织节点。"""
    return await service.delete_department(
        db, department_id=department_id, authorization=authorization, ip_address=extract_client_ip(request) if request else None
    )
