"""组织树查询、授权范围与写入事务。"""

import re

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.modules.identity.models import ROOT_DEPARTMENT_ID
from yuxi.modules.identity.permissions import AuthorizationContext
from yuxi.modules.identity.repositories.departments import DepartmentRepository
from yuxi.modules.identity.repositories.roles import RoleRepository
from yuxi.modules.identity.repositories.users import UserRepository
from yuxi.modules.identity.security import AuthUtils
from yuxi.modules.identity.services.authorization import lock_authorization
from yuxi.modules.identity.services.role_assignments import UserRoleAuthorizationError, replace_user_role_assignments
from yuxi.modules.identity.services.usernames import is_valid_phone_number
from yuxi.modules.identity.services.visibility import department_is_accessible, list_authorized_departments
from yuxi.modules.system.services.audit import log_operation
from yuxi.shared.errors import InvalidOperationError, OperationFailedError, PermissionDeniedError, ResourceNotFoundError


async def _ensure_department_access(db, authorization, permission_key, department_id) -> None:
    """确认组织节点位于当前功能权限的管理域内。"""
    if not await department_is_accessible(authorization, permission_key, department_id, db=db):
        raise ResourceNotFoundError("组织节点不存在")


async def get_departments(*, authorization: AuthorizationContext):
    """返回完整集团目录或当前授权子树及必要祖先。"""
    can_read_all = authorization.has_permission("department:read_all")
    if not can_read_all and (not authorization.has_permission("department:read")):
        raise PermissionDeniedError("缺少功能权限: department:read")
    return await list_authorized_departments(authorization)


async def get_department(db: AsyncSession, *, department_id: int, authorization: AuthorizationContext):
    """获取指定部门详情"""
    permission_key = "department:read_all" if authorization.has_permission("department:read_all") else "department:read"
    if not authorization.has_permission(permission_key):
        raise PermissionDeniedError("缺少功能权限: department:read")
    await _ensure_department_access(db, authorization, permission_key, department_id)
    department = await DepartmentRepository(db).get_with_user_count(department_id)
    if department is None:
        raise ResourceNotFoundError("组织节点不存在")
    return department


async def create_department(
    db: AsyncSession,
    *,
    authorization: AuthorizationContext,
    ip_address: str | None,
    name: str,
    description: str | None = None,
    parent_id: int | None = None,
    node_type: str = "department",
    admin_uid: str | None = None,
    admin_password: str | None = None,
    admin_phone: str | None = None,
):
    """在指定父节点下创建组织节点，可选地同时创建一个管理员账号"""
    authorization = await lock_authorization(db, authorization)
    dept_repo = DepartmentRepository(db)
    user_repo = UserRepository(db)
    current_user = authorization.user
    parent_id = parent_id if parent_id is not None else ROOT_DEPARTMENT_ID
    await _ensure_department_access(db, authorization, "department:create", parent_id)
    parent = await dept_repo.get_by_id(parent_id)
    if parent is None:
        raise InvalidOperationError("父级组织节点不存在")
    if await dept_repo.exists_sibling_name(parent.id, name):
        raise InvalidOperationError("同一父级下已存在同名组织节点")
    if admin_uid:
        for permission_key in ("user:create", "user:role_assign"):
            if not authorization.has_permission(permission_key):
                raise PermissionDeniedError(f"缺少功能权限: {permission_key}")
        if not admin_password:
            raise InvalidOperationError("创建管理员时必须提供密码")
        if not re.match("^[a-zA-Z0-9_]+$", admin_uid):
            raise InvalidOperationError("用户ID只能包含字母、数字和下划线")
        if len(admin_uid) < 3 or len(admin_uid) > 20:
            raise InvalidOperationError("用户ID长度必须在3-20个字符之间")
        if await user_repo.exists_by_uid(admin_uid):
            raise InvalidOperationError("用户ID已存在")
        if admin_phone:
            if not is_valid_phone_number(admin_phone):
                raise InvalidOperationError("手机号格式不正确")
            if await user_repo.exists_by_phone(admin_phone):
                raise InvalidOperationError("手机号已存在")
    elif admin_password or admin_phone:
        raise InvalidOperationError("创建管理员时必须提供用户ID")
    new_department = await dept_repo.create_child(db, name=name, description=description, parent=parent, node_type=node_type)
    if not admin_uid:
        await log_operation(db, current_user.id, "创建组织节点", f"创建组织节点: {name}", ip_address=ip_address)
        await db.commit()
        return {**new_department.to_dict(), "user_count": 0}
    await _ensure_department_access(db, authorization, "user:create", new_department.id)
    new_user = await user_repo.create_with_db(
        db,
        {
            "username": admin_uid,
            "uid": admin_uid,
            "phone_number": admin_phone,
            "password_hash": AuthUtils.hash_password(admin_password),
            "department_id": new_department.id,
        },
    )
    admin_role = await RoleRepository(db).get_by_code("admin")
    if admin_role is None or not admin_role.is_active:
        raise OperationFailedError("系统缺少有效的 admin 角色")
    try:
        await replace_user_role_assignments(
            db,
            authorization=authorization,
            target=new_user,
            assignments=[{"role_id": admin_role.id, "scope_mode": "inherit"}],
            check_existing=False,
        )
    except UserRoleAuthorizationError as error:
        raise PermissionDeniedError(str(error)) from error
    await log_operation(db, current_user.id, "创建组织节点", f"创建组织节点: {name}，并创建管理员: {admin_uid}", ip_address=ip_address)
    await db.commit()
    return {**new_department.to_dict(), "user_count": 1}


async def update_department(
    db: AsyncSession,
    *,
    department_id: int,
    authorization: AuthorizationContext,
    ip_address: str | None,
    name: str | None = None,
    description: str | None = None,
    parent_id: int | None = None,
    parent_id_provided: bool = False,
):
    """更新组织节点信息，并在父节点变化时移动整棵子树"""
    authorization = await lock_authorization(db, authorization)
    dept_repo = DepartmentRepository(db)
    current_user = authorization.user
    await _ensure_department_access(db, authorization, "department:update", department_id)
    department = await dept_repo.get_by_id(department_id)
    if not department:
        raise ResourceNotFoundError("组织节点不存在")
    move_requested = parent_id_provided
    target_parent = None
    target_parent_id = department.parent_id
    if move_requested:
        if department.id == ROOT_DEPARTMENT_ID:
            raise InvalidOperationError("集团根不允许移动")
        if parent_id is None:
            raise InvalidOperationError("父级组织节点不能为空")
        if parent_id == department.id:
            raise InvalidOperationError("组织节点不能移动到自身之下")
        await _ensure_department_access(db, authorization, "department:update", parent_id)
        target_parent = await dept_repo.get_by_id(parent_id)
        if target_parent is None:
            raise InvalidOperationError("父级组织节点不存在")
        if target_parent.path.startswith(department.path):
            raise InvalidOperationError("组织节点不能移动到自身后代之下")
        target_parent_id = target_parent.id
    target_name = name or department.name
    if target_name != department.name or target_parent_id != department.parent_id:
        if await dept_repo.exists_sibling_name(target_parent_id, target_name):
            raise InvalidOperationError("同一父级下已存在同名组织节点")
    if name:
        department.name = name
    if description is not None:
        department.description = description
    if target_parent is not None and target_parent.id != department.parent_id:
        await dept_repo.move_subtree(db, department, target_parent)
    await dept_repo.save(department)
    await log_operation(db, current_user.id, "更新部门", f"更新部门: {department.name}", ip_address=ip_address)
    await db.commit()
    await dept_repo.save(department, refresh=True)
    user_count = await dept_repo.count_users(department_id)
    return {**department.to_dict(), "user_count": user_count}


async def delete_department(db: AsyncSession, *, department_id: int, authorization: AuthorizationContext, ip_address: str | None):
    """删除组织节点"""
    authorization = await lock_authorization(db, authorization)
    dept_repo = DepartmentRepository(db)
    current_user = authorization.user
    await _ensure_department_access(db, authorization, "department:delete", department_id)
    department = await dept_repo.get_by_id(department_id)
    if not department:
        raise ResourceNotFoundError("组织节点不存在")
    if department.id == ROOT_DEPARTMENT_ID:
        raise InvalidOperationError("集团根不允许删除")
    if await dept_repo.count_children(department_id):
        raise InvalidOperationError("该组织节点下还有子节点，请先处理子节点")
    if await dept_repo.count_users(department_id):
        raise InvalidOperationError("该组织节点下还有直属用户，请先调整用户的组织归属")
    department_name = department.name
    await dept_repo.delete_empty_node(department)
    await log_operation(db, current_user.id, "删除组织节点", f"删除组织节点: {department_name}", ip_address=ip_address)
    await db.commit()
    return {"success": True, "message": "组织节点已删除"}
