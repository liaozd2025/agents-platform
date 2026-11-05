"""组织与初始身份管理用例的事务 Owner。"""

import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.modules.identity.models import ROOT_DEPARTMENT_ID, Department, User
from yuxi.modules.identity.repositories.departments import DepartmentRepository
from yuxi.modules.identity.repositories.users import UserRepository
from yuxi.modules.identity.security import AuthUtils
from yuxi.modules.identity.services.usernames import is_valid_phone_number
from yuxi.modules.system.services.audit import log_operation
from yuxi.shared.datetime import utc_now
from yuxi.shared.errors import InvalidOperationError, ResourceNotFoundError

_INITIALIZATION_LOCK_KEY = 0x59555849


class IdentityConflictError(Exception):
    """唯一身份或部门事实在提交时发生冲突。"""


class SystemAlreadyInitializedError(Exception):
    """系统初始化已由当前或并发请求完成。"""


@dataclass(frozen=True)
class DepartmentAdminCreation:
    """同一事务创建的部门和管理员。"""

    department: Department
    admin: User


async def list_managed_users_page(
    db: AsyncSession,
    *,
    offset: int,
    limit: int,
    is_superadmin: bool,
    visible_department_id: int | None,
    department_id: int | None,
    role: str | None,
    search: str | None,
    user_kind: str | None = "human",
    department_ids: list[int] | None = None,
    roles: list[str] | None = None,
) -> dict:
    """返回管理员可见范围内的用户分页。"""
    merged_roles = list(dict.fromkeys((roles or []) + ([role] if role is not None else [])))
    if is_superadmin:
        merged_department_ids = set(department_ids or ())
        if department_id is not None:
            merged_department_ids.add(department_id)
        effective_department_id = None
        effective_department_ids = sorted(merged_department_ids) or None
    else:
        # 非超级管理员的可见范围始终固定为本部门，多选仅作用于角色。
        effective_department_id = visible_department_id
        effective_department_ids = None
        if effective_department_id is None:
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
    rows, total = await UserRepository(db).list_page_with_department(
        offset=offset,
        limit=limit,
        department_id=effective_department_id,
        department_ids=effective_department_ids,
        roles=merged_roles or None,
        search=search,
        user_kind=user_kind,
    )
    items = []
    for user, department_name in rows:
        item = user.to_dict()
        item["department_name"] = department_name
        item["app_id"] = user.app_id
        items.append(item)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def create_department_with_admin(
    db: AsyncSession,
    *,
    name: str,
    description: str | None,
    admin_uid: str,
    admin_password: str,
    admin_phone: str | None,
    actor_user_id: int,
    ip_address: str | None = None,
) -> DepartmentAdminCreation:
    """原子创建部门、首位管理员和强制审计事实。"""

    if not re.fullmatch(r"[a-zA-Z0-9_]{3,20}", admin_uid):
        raise InvalidOperationError("用户ID必须为3-20位字母、数字或下划线")
    if admin_phone and not is_valid_phone_number(admin_phone):
        raise InvalidOperationError("手机号格式不正确")
    if await DepartmentRepository(db).exists_sibling_name(ROOT_DEPARTMENT_ID, name):
        raise InvalidOperationError("同一父级下已存在同名组织节点")
    password_hash = AuthUtils.hash_password(admin_password)
    try:
        try:
            department_repository = DepartmentRepository(db)
            parent = await department_repository.get_by_id(ROOT_DEPARTMENT_ID)
            if parent is None:
                raise IdentityConflictError("集团根组织不存在")
            department = await department_repository.create_child(
                db,
                name=name,
                description=description,
                parent=parent,
            )
            admin = await UserRepository(db).create(
                {
                    "username": admin_uid,
                    "uid": admin_uid,
                    "phone_number": admin_phone,
                    "password_hash": password_hash,
                    "department_id": department.id,
                },
                default_role_code="admin",
            )
        except IntegrityError as exc:
            raise IdentityConflictError("部门名称、管理员用户ID、用户名或手机号已存在") from exc

        await log_operation(
            db,
            actor_user_id,
            "创建部门",
            f"创建部门: {name}，并创建管理员: {admin_uid}",
            ip_address=ip_address,
        )
        await db.commit()
        return DepartmentAdminCreation(department=department, admin=admin)
    except Exception:
        await db.rollback()
        raise


async def initialize_system_admin(
    db: AsyncSession,
    *,
    uid: str,
    password: str,
    phone_number: str | None,
) -> DepartmentAdminCreation:
    """串行、原子地创建默认部门、超级管理员和初始化审计。"""

    password_hash = AuthUtils.hash_password(password)
    try:
        bind = db.get_bind()
        if bind.dialect.name == "postgresql":
            await db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": _INITIALIZATION_LOCK_KEY})

        if not await UserRepository(db).is_first_run():
            raise SystemAlreadyInitializedError("系统已经初始化，无法再次创建初始管理员")

        try:
            department = await DepartmentRepository(db).create_group_root(
                name="集团",
                description="系统初始化时创建的集团根节点",
            )
            admin = await UserRepository(db).create(
                {
                    "username": uid,
                    "uid": uid,
                    "phone_number": phone_number,
                    "avatar": None,
                    "password_hash": password_hash,
                    "department_id": department.id,
                    "last_login": utc_now(),
                },
                default_role_code="superadmin",
            )
        except IntegrityError as exc:
            raise IdentityConflictError("初始化身份事实与现有数据库约束冲突") from exc

        await log_operation(db, admin.id, "系统初始化", "创建超级管理员账户")
        await db.commit()
        return DepartmentAdminCreation(department=department, admin=admin)
    except SystemAlreadyInitializedError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise


async def update_department(db: AsyncSession, *, department_id: int, name: str | None = None, description: str | None = None) -> dict:
    """更新部门信息"""
    try:
        repository = DepartmentRepository(db)
        department = await repository.get_by_id(department_id)

        if not department:
            raise ResourceNotFoundError("部门不存在")

        updates = {}
        if name and name != department.name:
            existing = await repository.get_by_name(name)
            if existing:
                raise InvalidOperationError("部门名称已存在")
            updates["name"] = name

        if description is not None:
            updates["description"] = description

        department = await repository.update(department_id, updates)
        if department is None:
            raise ResourceNotFoundError("部门不存在")

        user_count = await repository.count_users(department_id)
        await db.commit()

        return {**department.to_dict(), "user_count": user_count}
    except IntegrityError as exc:
        await db.rollback()
        raise InvalidOperationError("部门名称或关联数据与现有约束冲突") from exc
    except BaseException:
        await db.rollback()
        raise


async def delete_department(db: AsyncSession, *, department_id: int) -> dict:
    """删除部门"""
    try:
        repository = DepartmentRepository(db)
        department = await repository.get_by_id(department_id)

        if not department:
            raise ResourceNotFoundError("部门不存在")

        if department.id == 1:  # 默认部门的ID为1
            raise InvalidOperationError("默认部门不允许删除")

        deletion = await repository.delete_and_migrate_users(department_id)
        if deletion is None:
            raise ResourceNotFoundError("部门不存在")

        await db.commit()

        return {"success": True, "message": "部门已删除"}
    except IntegrityError as exc:
        await db.rollback()
        raise InvalidOperationError("部门名称或关联数据与现有约束冲突") from exc
    except BaseException:
        await db.rollback()
        raise


async def list_departments(db: AsyncSession) -> list[dict]:
    """返回部门及其有效用户数量。"""
    return await DepartmentRepository(db).list_with_user_count()


async def get_department_detail(db: AsyncSession, *, department_id: int) -> dict | None:
    """读取单个部门详情；不存在由调用方映射为拒绝。"""
    return await DepartmentRepository(db).get_with_user_count(department_id)
