from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.infrastructure.observability.logging import logger
from yuxi.modules.identity.commands import UserCreate, UsernameValidation, UserPasswordChange, UserProfileUpdate, UserUpdate
from yuxi.modules.identity.models import ROOT_DEPARTMENT_ID, User
from yuxi.modules.identity.permissions.authorization import AuthorizationContext, build_authorization_context
from yuxi.modules.identity.repositories.departments import DepartmentRepository
from yuxi.modules.identity.repositories.users import UserRepository
from yuxi.modules.identity.security import AuthUtils
from yuxi.modules.identity.services.authorization import lock_authorization
from yuxi.modules.identity.services.role_assignments import (
    UserRoleAuthorizationError,
    UserRoleConflictError,
    has_active_role,
    replace_user_role_assignments,
    serialize_user,
)
from yuxi.modules.identity.services.usernames import generate_unique_uid, is_valid_phone_number, validate_username
from yuxi.modules.identity.services.visibility import (
    department_is_accessible,
    get_authorized_user,
    list_authorized_users_page,
    managed_user_target,
)
from yuxi.modules.system.services.audit import log_operation
from yuxi.shared.errors import InvalidOperationError, OperationFailedError, PermissionDeniedError, ResourceNotFoundError, StateConflictError


def _raise_user_role_error(error: ValueError) -> None:
    """将角色分配校验错误转换为明确的业务拒绝类型。"""
    if isinstance(error, UserRoleAuthorizationError):
        raise PermissionDeniedError(str(error)) from error
    if isinstance(error, UserRoleConflictError):
        raise StateConflictError(str(error)) from error
    raise InvalidOperationError(str(error)) from error


async def _get_authorized_user(
    db: AsyncSession, authorization: AuthorizationContext, permission_key: str, user_id: int, *, include_app: bool = False
) -> User:
    """读取管理域内用户，缺失或越权统一返回 404。"""
    user = await get_authorized_user(db, authorization, permission_key, user_id, include_app=include_app)
    if user is None:
        raise ResourceNotFoundError("用户不存在")
    return user


async def _ensure_department_access(db: AsyncSession, authorization: AuthorizationContext, permission_key: str, department_id: int) -> None:
    """确认目标组织节点存在且位于当前权限的数据范围内。"""
    if not await department_is_accessible(authorization, permission_key, department_id, db=db):
        raise ResourceNotFoundError("组织节点不存在")


def _serialize_login_response(user: User, access_token: str, department_name: str | None = None) -> dict:
    """序列化登录令牌、多角色和有效权限。"""
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": user.id,
        **serialize_user(user, department_name),
        "effective_permissions": list(build_authorization_context(user).effective_permissions),
    }


async def read_users_me(*, authorization: AuthorizationContext, db: AsyncSession):
    """获取当前登录用户的个人信息"""
    current_user = authorization.user
    department_name = None
    if current_user.department_id:
        department_name = await DepartmentRepository(db).get_name_by_id(current_user.department_id)
    return {**serialize_user(current_user, department_name), "effective_permissions": list(authorization.effective_permissions)}


async def update_profile(*, profile_data: UserProfileUpdate, ip_address: str | None, current_user: User, db: AsyncSession):
    """更新当前用户的个人资料"""
    update_details = []
    user_repository = UserRepository(db)
    if profile_data.username is not None:
        is_valid, error_msg = validate_username(profile_data.username)
        if not is_valid:
            raise InvalidOperationError(error_msg)
        existing_user = await user_repository.get_by_username(profile_data.username, exclude_user_id=current_user.id)
        if existing_user:
            raise InvalidOperationError("用户名已存在")
        current_user.username = profile_data.username
        update_details.append(f"用户名: {profile_data.username}")
    if profile_data.phone_number is not None:
        if profile_data.phone_number and (not is_valid_phone_number(profile_data.phone_number)):
            raise InvalidOperationError("手机号格式不正确")
        if profile_data.phone_number:
            existing_phone = await user_repository.get_by_phone_excluding(profile_data.phone_number, current_user.id)
            if existing_phone:
                raise InvalidOperationError("手机号已被其他用户使用")
        current_user.phone_number = profile_data.phone_number
        update_details.append(f"手机号: {profile_data.phone_number or '已清空'}")
    await user_repository.save(current_user)
    if update_details:
        await log_operation(db, current_user.id, "更新个人资料", f"更新个人资料: {', '.join(update_details)}", ip_address=ip_address)
    await db.commit()
    return serialize_user(current_user)


async def change_own_password(*, password_data: UserPasswordChange, ip_address: str | None, current_user: User, db: AsyncSession):
    """校验原密码后更新当前用户的登录密码。

    原密码校验是安全底线：令牌泄露时，攻击者不能直接改密接管账号。
    改密成功后清零登录失败计数与锁定状态，避免旧锁定继续挡住新密码登录；
    并写入「密码已更新」操作日志——该文案同时是批量运维判断「密码是否被改过」的判据。
    """
    user_repository = UserRepository(db)
    if not AuthUtils.verify_password(current_user.password_hash, password_data.old_password):
        raise InvalidOperationError("原密码不正确")
    if AuthUtils.verify_password(current_user.password_hash, password_data.new_password):
        raise InvalidOperationError("新密码不能与原密码相同")
    current_user.password_hash = AuthUtils.hash_password(password_data.new_password)
    current_user.reset_failed_login()
    await user_repository.save(current_user)
    await log_operation(db, current_user.id, "修改密码", "密码已更新", ip_address=ip_address)
    await db.commit()
    logger.info(f"用户自助修改密码成功：user_id={current_user.id}")
    return {"message": "密码修改成功"}


async def create_user(*, user_data: UserCreate, ip_address: str | None, authorization: AuthorizationContext, db: AsyncSession):
    """在当前管理域内创建新用户。"""
    authorization = await lock_authorization(db, authorization)
    if not authorization.has_permission("user:create"):
        raise PermissionDeniedError("缺少功能权限: user:create")
    current_user = authorization.user
    user_repo = UserRepository(db)
    is_valid, error_msg = validate_username(user_data.username)
    if not is_valid:
        raise InvalidOperationError(error_msg)
    users = await user_repo.list_users()
    if any(u.username == user_data.username for u in users):
        raise InvalidOperationError("用户名已存在")
    if user_data.phone_number:
        if await user_repo.exists_by_phone(user_data.phone_number):
            raise InvalidOperationError("手机号已存在")
    existing_uids = await user_repo.get_all_uids()
    uid = generate_unique_uid(user_data.username, existing_uids)
    hashed_password = AuthUtils.hash_password(user_data.password)
    requested_assignments = user_data.role_assignments
    if requested_assignments is not None and (not authorization.has_permission("user:role_assign")):
        raise PermissionDeniedError("缺少功能权限: user:role_assign")
    department_id = user_data.department_id
    if department_id is None:
        department_id = current_user.department_id or ROOT_DEPARTMENT_ID
    await _ensure_department_access(db, authorization, "user:create", department_id)
    new_user = await user_repo.create_with_db(
        db,
        {
            "username": user_data.username,
            "uid": uid,
            "phone_number": user_data.phone_number,
            "password_hash": hashed_password,
            "department_id": department_id,
        },
    )
    if requested_assignments is not None:
        try:
            await replace_user_role_assignments(
                db,
                authorization=authorization,
                target=new_user,
                assignments=[item.model_dump() for item in requested_assignments],
                reason=user_data.reason,
                check_existing=False,
            )
        except ValueError as error:
            _raise_user_role_error(error)
    await log_operation(
        db,
        current_user.id,
        "创建用户",
        f"创建用户: {user_data.username}, 角色: {', '.join(item.role.name for item in new_user.role_assignments)}",
        ip_address=ip_address,
    )
    await db.commit()
    new_user = await user_repo.get_by_id_with_db(db, new_user.id)
    if new_user is None:
        raise OperationFailedError("用户创建后读取失败")
    return serialize_user(new_user)


def _serialize_managed_user(user: User, authorization: AuthorizationContext, department_name: str | None = None) -> dict:
    """返回管理页身份来源和当前删除能力；APP 不携带企业角色。"""
    target = managed_user_target(user)
    payload = (
        {**user.to_dict(), "department_name": None, "roles": []} if user.user_kind == "end_user" else serialize_user(user, department_name)
    )
    return {
        **payload,
        "app_id": user.app_id,
        "can_delete": target is not None
        and authorization.allows("user:delete", target)
        and user.id != authorization.user.id
        and not has_active_role(user, "superadmin"),
    }


async def read_users_page(
    *,
    offset: int = 0,
    limit: int = 50,
    search: str | None = None,
    department_id: int | None = None,
    department_ids: list[int] | None = None,
    role: str | None = None,
    roles: list[str] | None = None,
    user_kind: str = "human",
    authorization: AuthorizationContext,
    db: AsyncSession | None = None,
):
    """按真人或 APP owner 的当前多角色管理范围返回分页。"""
    page_result = await list_authorized_users_page(
        authorization,
        "user:read",
        skip=offset,
        limit=limit,
        department_id=department_id,
        department_ids=department_ids,
        role=role,
        roles=roles,
        user_kind=user_kind,
        keyword=search.strip() if search else None,
        db=db,
    )
    if page_result is None:
        raise ResourceNotFoundError("组织节点不存在")
    rows, total = page_result
    return {
        "items": [_serialize_managed_user(user, authorization, department_name) for user, department_name in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def read_users(
    *,
    skip: int = 0,
    limit: int = 100,
    department_id: int | None = None,
    department_ids: list[int] | None = None,
    direct: bool = False,
    keyword: str | None = None,
    role: str | None = None,
    roles: list[str] | None = None,
    user_kind: str = "human",
    authorization: AuthorizationContext,
    db: AsyncSession | None = None,
):
    """返回管理域内用户；部门和角色多选在授权交集内执行。"""
    page_result = await list_authorized_users_page(
        authorization,
        "user:read",
        skip=skip,
        limit=limit,
        department_id=department_id,
        department_ids=department_ids,
        direct=direct,
        keyword=keyword,
        role=role,
        roles=roles,
        user_kind=user_kind,
        db=db,
    )
    if page_result is None:
        raise ResourceNotFoundError("组织节点不存在")
    visible_rows, total = page_result
    logger.info("用户列表分页查询完成：skip=%s limit=%s total=%s", skip, limit, total)
    return [_serialize_managed_user(user, authorization, department_name) for user, department_name in visible_rows], total


async def read_user_access_options(
    *,
    skip: int = 0,
    limit: int = 100,
    keyword: str | None = None,
    department_id: int | None = None,
    direct: bool = False,
    authorization: AuthorizationContext,
    db: AsyncSession | None = None,
):
    """返回管理域内的候选用户，支持关键字远程检索。

    授权范围过滤与关键字筛选均在仓储层以 SQL 完成，避免把全量用户
    载入内存后逐个判定；返回总数供接口适配分页。
    """
    page_result = await list_authorized_users_page(
        authorization, "user:read", skip=skip, limit=limit, department_id=department_id, direct=direct, keyword=keyword, db=db
    )
    if page_result is None:
        raise ResourceNotFoundError("组织节点不存在")
    visible_rows, total = page_result
    return [
        {"uid": user.uid, "username": user.username, "department_id": user.department_id, "department_name": dept_name}
        for user, dept_name in visible_rows
    ], total


async def read_user(*, user_id: int, authorization: AuthorizationContext, db: AsyncSession):
    """读取真人或位于 owner 管理域内的 APP 用户。"""
    user = await _get_authorized_user(db, authorization, "user:read", user_id, include_app=True)
    return _serialize_managed_user(user, authorization)


async def update_user(
    *, user_id: int, user_data: UserUpdate, ip_address: str | None, authorization: AuthorizationContext, db: AsyncSession
):
    """修改管理域内用户，并限制新的组织归属。"""
    authorization = await lock_authorization(db, authorization)
    user_repository = UserRepository(db)
    required_permissions = []
    if user_data.role_assignments is not None:
        required_permissions.append("user:role_assign")
    if (
        any(
            value is not None
            for value in (user_data.username, user_data.password, user_data.phone_number, user_data.avatar, user_data.department_id)
        )
        or not required_permissions
    ):
        required_permissions.append("user:update")
    for permission_key in required_permissions:
        if not authorization.has_permission(permission_key):
            raise PermissionDeniedError(f"缺少功能权限: {permission_key}")
    user = await _get_authorized_user(db, authorization, required_permissions[0], user_id)
    for permission_key in required_permissions[1:]:
        user = await _get_authorized_user(db, authorization, permission_key, user_id)
    current_user = authorization.user
    update_details = []
    if user_data.username is not None:
        existing_user = await user_repository.get_by_username(user_data.username, exclude_user_id=user_id)
        if existing_user:
            raise InvalidOperationError("用户名已存在")
        user.username = user_data.username
        update_details.append(f"用户名: {user_data.username}")
    if user_data.password is not None:
        user.password_hash = AuthUtils.hash_password(user_data.password)
        update_details.append("密码已更新")
    if user_data.phone_number is not None:
        user.phone_number = user_data.phone_number
        update_details.append(f"手机号: {user_data.phone_number or '已清空'}")
    if user_data.avatar is not None:
        user.avatar = user_data.avatar
        update_details.append(f"头像: {user_data.avatar or '已清空'}")
    if user_data.department_id is not None and user_data.department_id != user.department_id:
        await _ensure_department_access(db, authorization, "user:update", user_data.department_id)
        user.department_id = user_data.department_id
        update_details.append(f"部门ID: {user_data.department_id}")
    if user_data.role_assignments is not None:
        try:
            await replace_user_role_assignments(
                db,
                authorization=authorization,
                target=user,
                assignments=[item.model_dump() for item in user_data.role_assignments],
                reason=user_data.reason,
            )
        except ValueError as error:
            _raise_user_role_error(error)
        update_details.append("角色分配")
    await user_repository.save(user)
    await log_operation(db, current_user.id, "更新用户", f"更新用户ID {user_id}: {', '.join(update_details)}", ip_address=ip_address)
    await db.commit()
    return serialize_user(user)


async def delete_user(*, user_id: int, ip_address: str | None, authorization: AuthorizationContext, db: AsyncSession):
    """软删除管理域内用户。"""
    authorization = await lock_authorization(db, authorization)
    user_repository = UserRepository(db)
    current_user = authorization.user
    user = await _get_authorized_user(db, authorization, "user:delete", user_id, include_app=True)
    if has_active_role(user, "superadmin"):
        raise InvalidOperationError("不能删除超级管理员账户")
    if user.id == current_user.id:
        raise InvalidOperationError("不能删除自己的账户")
    if user.is_deleted:
        raise InvalidOperationError("该用户已经被删除")
    deletion_detail = f"删除用户: {user.username}, ID: {user.id}, 角色: {', '.join(item.role.name for item in user.role_assignments)}"
    await user_repository.delete_for_admin(user)
    await log_operation(db, current_user.id, "删除用户", deletion_detail, ip_address=ip_address)
    await db.commit()
    return {"success": True, "message": "用户已删除"}


async def validate_username_and_generate_uid(
    *, validation_data: UsernameValidation, _authorization: AuthorizationContext, db: AsyncSession
):
    """验证用户名格式并生成可用的user_id"""
    is_valid, error_msg = validate_username(validation_data.username)
    if not is_valid:
        raise InvalidOperationError(error_msg)
    user_repository = UserRepository(db)
    existing_user = await user_repository.get_by_username(validation_data.username)
    if existing_user:
        raise InvalidOperationError("用户名已存在")
    existing_uids = await user_repository.get_all_uids()
    uid = generate_unique_uid(validation_data.username, existing_uids)
    return {"username": validation_data.username, "uid": uid, "is_available": True}


async def check_uid_availability(*, uid: str, _authorization: AuthorizationContext, db: AsyncSession):
    """检查 uid 是否可用"""
    return {"uid": uid, "is_available": not await UserRepository(db).exists_by_uid(uid)}


async def impersonate_user(*, user_id: int, ip_address: str | None, authorization: AuthorizationContext, db: AsyncSession):
    """在当前管理域内模拟其他用户登录。"""
    authorization = await lock_authorization(db, authorization)
    current_user = authorization.user
    target_user = await _get_authorized_user(db, authorization, "user:impersonate", user_id)
    if has_active_role(target_user, "superadmin"):
        raise PermissionDeniedError("不能模拟超级管理员账户")
    token_data = {"sub": str(target_user.id)}
    access_token = AuthUtils.create_access_token(token_data)
    department_name = None
    if target_user.department_id:
        department_name = await DepartmentRepository(db).get_name_by_id(target_user.department_id)
    target_user = await UserRepository().get_by_id_with_db(db, target_user.id)
    if target_user is None:
        raise ResourceNotFoundError("用户不存在")
    await log_operation(db, current_user.id, "⚠️ 危险操作-模拟用户", f"模拟用户: {target_user.username}", ip_address=ip_address)
    await db.commit()
    logger.warning(f"⚠️ [危险操作] 超级管理员 {current_user.username} 模拟登录用户: {target_user.username}")
    return _serialize_login_response(target_user, access_token, department_name)
