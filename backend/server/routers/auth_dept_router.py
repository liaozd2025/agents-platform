"""
组织机构管理路由
提供组织节点的增删改查接口，仅超级管理员可访问
"""

import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete as sqlalchemy_delete, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import ROOT_DEPARTMENT_ID, APIKey, Department, User
from yuxi.repositories.department_repository import DepartmentRepository
from yuxi.repositories.user_repository import UserRepository
from server.utils.auth_middleware import get_superadmin_user, get_admin_user, get_db
from yuxi.utils.auth_utils import AuthUtils
from yuxi.services.operation_log_service import log_operation
from yuxi.services.user_identity_service import is_valid_phone_number

# 创建路由器
department = APIRouter(prefix="/departments", tags=["department"])


# =============================================================================
# === 请求和响应模型 ===
# =============================================================================


class DepartmentCreate(BaseModel):
    """创建组织节点请求"""

    name: str
    description: str | None = None
    parent_id: int | None = None  # 不传时挂在集团根下
    node_type: Literal["group", "company", "department"] = "department"
    # 可选的管理员信息；填写时创建的是全局管理员，其权限不限于该节点
    admin_uid: str | None = None
    admin_password: str | None = Field(default=None, min_length=8)
    admin_phone: str | None = None


class DepartmentUpdate(BaseModel):
    """更新组织节点请求"""

    name: str | None = None
    description: str | None = None


class DepartmentResponse(BaseModel):
    """组织节点响应"""

    id: int
    name: str
    description: str | None = None
    created_at: str
    parent_id: int | None = None
    node_type: str
    user_count: int = 0


# =============================================================================
# === 部门管理路由 ===
# =============================================================================


@department.get("", response_model=list[DepartmentResponse])
async def get_departments(current_user: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)):
    """获取所有部门列表（管理员可访问）"""
    dept_repo = DepartmentRepository()
    return await dept_repo.list_with_user_count()


@department.get("/{department_id}", response_model=DepartmentResponse)
async def get_department(
    department_id: int, current_user: User = Depends(get_superadmin_user), db: AsyncSession = Depends(get_db)
):
    """获取指定部门详情"""
    result = await db.execute(select(Department).filter(Department.id == department_id))
    department = result.scalar_one_or_none()

    if not department:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="组织节点不存在")

    # 获取部门下用户数量
    user_count_result = await db.execute(
        select(func.count(User.id)).filter(User.department_id == department_id, User.is_deleted == 0)
    )
    user_count = user_count_result.scalar()

    return {**department.to_dict(), "user_count": user_count}


@department.post("", response_model=DepartmentResponse, status_code=status.HTTP_201_CREATED)
async def create_department(
    department_data: DepartmentCreate,
    request: Request,
    current_user: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """在指定父节点下创建组织节点，可选地同时创建一个管理员账号"""
    dept_repo = DepartmentRepository()
    user_repo = UserRepository()

    parent_id = department_data.parent_id if department_data.parent_id is not None else ROOT_DEPARTMENT_ID
    parent = await dept_repo.get_by_id(parent_id)
    if parent is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="父级组织节点不存在")

    # 名称只要求同级唯一：不同分子公司可以各有一个「人力资源部」
    if await dept_repo.exists_sibling_name(parent.id, department_data.name):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="同一父级下已存在同名组织节点")

    admin_uid = department_data.admin_uid
    admin_phone = department_data.admin_phone
    if admin_uid:
        if not department_data.admin_password:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="创建管理员时必须提供密码")

        if not re.match(r"^[a-zA-Z0-9_]+$", admin_uid):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="用户ID只能包含字母、数字和下划线",
            )

        if len(admin_uid) < 3 or len(admin_uid) > 20:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="用户ID长度必须在3-20个字符之间",
            )

        if await user_repo.exists_by_uid(admin_uid):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="用户ID已存在",
            )

        if admin_phone:
            if not is_valid_phone_number(admin_phone):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="手机号格式不正确")
            if await user_repo.exists_by_phone(admin_phone):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="手机号已存在",
                )
    elif department_data.admin_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="创建管理员时必须提供用户ID")

    new_department = await dept_repo.create_child(
        name=department_data.name,
        description=department_data.description,
        parent=parent,
        node_type=department_data.node_type,
    )

    if not admin_uid:
        await log_operation(
            db, current_user.id, "创建组织节点", f"创建组织节点: {department_data.name}", request
        )
        return {**new_department.to_dict(), "user_count": 0}

    await user_repo.create(
        {
            "username": admin_uid,
            "uid": admin_uid,
            "phone_number": admin_phone,
            "password_hash": AuthUtils.hash_password(department_data.admin_password),
            "role": "admin",
            "department_id": new_department.id,
        }
    )

    await log_operation(
        db,
        current_user.id,
        "创建组织节点",
        f"创建组织节点: {department_data.name}，并创建管理员: {admin_uid}",
        request,
    )

    return {**new_department.to_dict(), "user_count": 1}


@department.put("/{department_id}", response_model=DepartmentResponse)
async def update_department(
    department_id: int,
    department_data: DepartmentUpdate,
    request: Request,
    current_user: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """更新部门信息"""
    result = await db.execute(select(Department).filter(Department.id == department_id))
    department = result.scalar_one_or_none()

    if not department:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="组织节点不存在")

    # 如果要修改名称，检查同级下是否已有同名节点（改名前已确认与自身不同名，无需排除自身）
    if department_data.name and department_data.name != department.name:
        if await DepartmentRepository().exists_sibling_name(department.parent_id, department_data.name):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="同一父级下已存在同名组织节点")
        department.name = department_data.name

    if department_data.description is not None:
        department.description = department_data.description

    await db.commit()
    await db.refresh(department)

    # 记录操作
    await log_operation(db, current_user.id, "更新部门", f"更新部门: {department.name}", request)

    # 获取部门下用户数量
    user_count_result = await db.execute(
        select(func.count(User.id)).filter(User.department_id == department_id, User.is_deleted == 0)
    )
    user_count = user_count_result.scalar()

    return {**department.to_dict(), "user_count": user_count}


@department.delete("/{department_id}", status_code=status.HTTP_200_OK)
async def delete_department(
    department_id: int,
    request: Request,
    current_user: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """删除组织节点"""
    result = await db.execute(select(Department).filter(Department.id == department_id))
    department = result.scalar_one_or_none()

    if not department:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="组织节点不存在")

    if department.id == ROOT_DEPARTMENT_ID:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="集团根不允许删除")

    # 不做级联删除，也不把子节点自动上提：静默重排组织结构比报错更危险
    child_count_result = await db.execute(
        select(func.count(Department.id)).filter(Department.parent_id == department_id)
    )
    if child_count_result.scalar():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该组织节点下还有子节点，请先处理子节点")

    department_name = department.name
    result = await db.execute(select(User).filter(User.department_id == department_id))
    department_users = result.scalars().all()

    if department_users:
        for user in department_users:
            user.department_id = ROOT_DEPARTMENT_ID  # 被删除节点的用户回落集团根

    await db.execute(sqlalchemy_delete(APIKey).where(APIKey.department_id == department_id))
    await db.delete(department)
    await db.commit()

    # 记录操作
    if department_users:
        detail = f"删除组织节点: {department_name}，回落 {len(department_users)} 个用户到集团根"
    else:
        detail = f"删除组织节点: {department_name}"
    await log_operation(db, current_user.id, "删除组织节点", detail, request)

    return {"success": True, "message": "组织节点已删除"}
