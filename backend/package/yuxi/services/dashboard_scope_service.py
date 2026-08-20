"""Dashboard 组织数据范围与资源授权主体。"""

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import and_, false, or_, true
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.permissions.authorization import AuthorizationContext, AuthorizationTarget, parse_department_ancestor_ids
from yuxi.repositories.department_repository import DepartmentRepository
from yuxi.services.user_management_service import (
    department_is_accessible,
    list_authorized_departments,
    list_authorized_users,
)


def dashboard_visibility_filter(
    authorization: AuthorizationContext,
    path_column: Any,
    *,
    owner_user_id_column: Any = None,
    owner_uid_column: Any = None,
) -> Any:
    """把同一角色分配的 Dashboard 数据范围转换为历史快照条件。"""

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


async def dashboard_history_filter(
    db: AsyncSession,
    authorization: AuthorizationContext,
    path_column: Any,
    department_id: int | None,
    *,
    owner_user_id_column: Any = None,
    owner_uid_column: Any = None,
) -> Any:
    """生成历史事件可见条件，并拒绝越出当前管理域的筛选目标。"""

    if department_id is not None and not await department_is_accessible(
        authorization,
        "dashboard:view",
        department_id,
        db=db,
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="组织节点不存在")

    visibility = dashboard_visibility_filter(
        authorization,
        path_column,
        owner_user_id_column=owner_user_id_column,
        owner_uid_column=owner_uid_column,
    )
    if department_id is None:
        return visibility
    return and_(visibility, path_column.like(f"%/{department_id}/%"))


async def dashboard_resource_subjects(
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
