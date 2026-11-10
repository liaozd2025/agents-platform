"""认证、用户管理与 CLI 浏览器登录授权路由。"""

import re
from typing import Literal

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.api.dependencies.auth import get_authorization_context, get_db, get_required_user, require_permission
from yuxi.api.middleware.access_log import extract_client_ip
from yuxi.api.routers.identity.oidc import (
    get_oidc_config_handler,
    oidc_callback_handler,
    oidc_exchange_code_handler,
    oidc_login_url_handler,
    oidc_logout_handler,
)
from yuxi.api.uploads import read_upload_with_limit
from yuxi.modules.identity.commands import UserCreate, UserPasswordChange, UserProfileUpdate, UserUpdate
from yuxi.modules.identity.models import User
from yuxi.modules.identity.permissions import AuthorizationContext
from yuxi.modules.identity.security import AuthUtils
from yuxi.modules.identity.services import management as enterprise_management
from yuxi.modules.identity.services import users as user_service
from yuxi.modules.identity.services.administration import IdentityConflictError, SystemAlreadyInitializedError, initialize_system_admin
from yuxi.modules.identity.services.cli_auth import (
    CLI_AUTH_POLL_INTERVAL_SECONDS,
    CLI_AUTH_SESSION_TTL_SECONDS,
    CLIAuthError,
    approve_cli_auth_session,
    create_cli_auth_session,
    exchange_cli_auth_token,
    get_cli_auth_session_for_user,
)
from yuxi.modules.identity.services.login import login_result
from yuxi.modules.identity.services.oa import (
    MAX_OA_ACCOUNT_LENGTH,
    MAX_OA_TOKEN_LENGTH,
    exchange_oa_account_handler,
    exchange_oa_token_handler,
)
from yuxi.modules.identity.services.password_login import PasswordLoginRejected, login_with_password
from yuxi.modules.identity.services.usernames import is_valid_phone_number

auth = APIRouter(prefix="/auth", tags=["authentication"])


class UserRoleResponse(BaseModel):
    """用户响应中的完整角色分配。"""

    assignment_id: int | None = None
    id: int
    code: str
    name: str
    is_builtin: bool
    is_active: bool
    scope_mode: str
    default_scope_type: str
    default_department_ids: list[int]
    override_scope_type: str | None = None
    override_department_ids: list[int]
    effective_scope_type: str
    effective_department_ids: list[int]


class Token(BaseModel):
    access_token: str
    token_type: str
    user_id: int
    username: str
    display_name: str | None = None
    uid: str
    phone_number: str | None = None
    avatar: str | None = None
    department_id: int | None = None
    department_name: str | None = None
    roles: list["UserRoleResponse"] = Field(default_factory=list)
    effective_permissions: list[str] = Field(default_factory=list)
    oa_profile: dict[str, str | None] | None = None


class UserResponse(BaseModel):
    id: int
    username: str
    display_name: str | None = None
    uid: str
    phone_number: str | None = None
    avatar: str | None = None
    department_id: int | None = None
    department_name: str | None = None
    created_at: str
    last_login: str | None = None
    roles: list[UserRoleResponse] = Field(default_factory=list)
    user_kind: Literal["human", "end_user"] = "human"
    app_id: str | None = None
    can_delete: bool | None = None


class ManagedUserResponse(UserResponse):
    """用户管理列表中的身份类型与 APP 来源。"""

    user_kind: Literal["human", "end_user"]
    app_id: str | None = None


class UserPageResponse(BaseModel):
    items: list[UserResponse]
    total: int
    limit: int
    offset: int


class UserAccessOption(BaseModel):
    uid: str
    username: str
    display_name: str | None = None
    department_id: int | None = None
    department_name: str | None = None


class InitializeAdmin(BaseModel):
    uid: str
    password: str = Field(min_length=8)
    phone_number: str | None = None


class UsernameValidation(BaseModel):
    username: str


class UidGeneration(BaseModel):
    username: str
    uid: str
    is_available: bool


class OIDCConfigResponse(BaseModel):
    """OIDC 配置响应"""

    enabled: bool
    login_url: str | None = None
    provider_name: str | None = "OIDC登录"


class CLIAuthSessionCreate(BaseModel):
    key_name: str | None = Field(default=None, max_length=100)


class CLIAuthTokenRequest(BaseModel):
    device_code: str


class CLIAuthSessionCreateResponse(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class CLIAuthSessionResponse(BaseModel):
    user_code: str
    status: str
    key_name: str
    created_at: str
    expires_at: str
    approved_at: str | None = None


class CLIAuthApproveResponse(BaseModel):
    user_code: str
    status: str
    approved_at: str | None = None


class CLIAuthTokenResponse(BaseModel):
    api_key: dict
    secret: str
    user: dict


class CurrentUserResponse(UserResponse):
    """当前登录用户及其请求级有效功能权限。"""

    effective_permissions: list[str] = Field(default_factory=list)


@auth.post("/token", response_model=Token)
async def login_for_access_token(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    """适配密码登录表单、拒绝原因与 HTTP 响应。"""
    try:
        result = await login_with_password(db, form_data.username, form_data.password, extract_client_ip(request))
    except PasswordLoginRejected as error:
        headers = {"WWW-Authenticate": "Bearer"}
        if error.reason == "rate_limited":
            code, detail = (429, "登录尝试过于频繁，请稍后再试")
            headers = {"Retry-After": str(error.remaining_seconds)}
        elif error.reason == "deleted":
            code, detail = (403, "该账户已注销")
        elif error.reason in {"locked", "newly_locked"}:
            code = 423
            remaining = error.remaining_seconds
            detail = (
                f"登录被锁定，请等待 {remaining} 秒后再试" if error.reason == "locked" else f"由于多次登录失败，账户已被锁定 {remaining} 秒"
            )
            headers["X-Lock-Remaining"] = str(remaining)
        else:
            code = 401
            detail = "登录标识或密码错误" if error.reason == "unknown_account" else "用户名或密码错误"
        raise HTTPException(status_code=code, detail=detail, headers=headers) from error
    return result


@auth.post("/cli/sessions", response_model=CLIAuthSessionCreateResponse)
async def create_cli_session(data: CLIAuthSessionCreate, db: AsyncSession = Depends(get_db)):
    """创建 CLI 设备授权会话，返回轮询参数。"""
    session, device_code = await create_cli_auth_session(db, key_name=data.key_name)
    return CLIAuthSessionCreateResponse(
        device_code=device_code,
        user_code=session.user_code,
        verification_uri="/auth/cli/authorize",
        expires_in=CLI_AUTH_SESSION_TTL_SECONDS,
        interval=CLI_AUTH_POLL_INTERVAL_SECONDS,
    )


@auth.get("/cli/sessions/{user_code}", response_model=CLIAuthSessionResponse)
async def get_cli_session(user_code: str, _current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    """读取当前用户创建的 CLI 授权会话状态。"""
    try:
        session = await get_cli_auth_session_for_user(db, user_code)
    except CLIAuthError as exc:
        _raise_cli_auth_error(exc)
    return CLIAuthSessionResponse(**session.to_dict())


@auth.post("/cli/sessions/{user_code}/approve", response_model=CLIAuthApproveResponse)
async def approve_cli_session(user_code: str, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    """当前用户批准指定 CLI 设备的授权请求。"""
    try:
        session = await approve_cli_auth_session(db, user_code, current_user)
    except CLIAuthError as exc:
        _raise_cli_auth_error(exc)
    return CLIAuthApproveResponse(**session.to_dict())


@auth.post("/cli/sessions/token", response_model=CLIAuthTokenResponse)
async def exchange_cli_session_token(data: CLIAuthTokenRequest, db: AsyncSession = Depends(get_db)):
    """设备凭 device_code 兑换 API Key。"""
    try:
        return await exchange_cli_auth_token(db, data.device_code)
    except CLIAuthError as exc:
        _raise_cli_auth_error(exc)


@auth.get("/check-first-run")
async def check_first_run():
    """报告系统是否尚未初始化管理员。"""
    return {"first_run": await user_service.is_first_run()}


@auth.post("/initialize", response_model=Token)
async def initialize_admin(admin_data: InitializeAdmin, db: AsyncSession = Depends(get_db)):
    """初始化系统管理员并直接返回登录令牌。"""
    if not re.match("^[a-zA-Z0-9_]+$", admin_data.uid):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户ID只能包含字母、数字和下划线")
    if len(admin_data.uid) < 3 or len(admin_data.uid) > 20:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户ID长度必须在3-20个字符之间")
    if admin_data.phone_number and (not is_valid_phone_number(admin_data.phone_number)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="手机号格式不正确")
    try:
        created = await initialize_system_admin(db, uid=admin_data.uid, password=admin_data.password, phone_number=admin_data.phone_number)
    except SystemAlreadyInitializedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except IdentityConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    new_admin = created.admin
    token_data = {"sub": str(new_admin.id)}
    access_token = AuthUtils.create_access_token(token_data)
    return login_result(new_admin, access_token, created.department.name)


def parse_csv_ints(raw: str | None) -> list[int] | None:
    """解析组织多选；非法参数拒绝，不能悄悄扩大为未筛选。"""
    if not raw:
        return None
    try:
        values = {int(part.strip()) for part in raw.split(",")}
        if any(value < 1 for value in values):
            raise ValueError
    except ValueError as error:
        raise HTTPException(status_code=422, detail="department_ids 必须为正整数列表") from error
    return sorted(values)


def parse_csv_roles(raw: str | None) -> list[str] | None:
    """保留企业自定义角色 code，角色存在性由仓储筛选决定。"""
    if not raw:
        return None
    values = [part.strip() for part in raw.split(",")]
    if any(not value or len(value) > 64 for value in values):
        raise HTTPException(status_code=422, detail="roles 必须为非空角色标识列表")
    return list(dict.fromkeys(values))


@auth.post("/upload-avatar")
async def upload_user_avatar(
    file: UploadFile = File(...), current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)
):
    """上传用户头像"""
    try:
        content = await read_upload_with_limit(file, max_size_bytes=5 * 1024 * 1024, too_large_message="文件大小不能超过5MB")
        avatar_url = await user_service.upload_avatar(db, current_user=current_user, content=content)
        return {"success": True, "avatar_url": avatar_url, "message": "头像上传成功"}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"头像上传失败: {str(e)}") from e


@auth.get("/oidc/config", response_model=OIDCConfigResponse)
async def get_oidc_config():
    """获取 OIDC 配置（供前端使用）"""
    return await get_oidc_config_handler()


@auth.get("/oidc/login-url")
async def get_oidc_login_url(request: Request, response: Response):
    """获取 OIDC 登录 URL"""
    return await oidc_login_url_handler(request, response)


@auth.get("/oidc/callback", response_class=RedirectResponse)
async def oidc_callback(
    request: Request,
    code: str = Query(default="", max_length=2048),
    state: str = Query(default="", max_length=128),
    error: str = Query(default="", max_length=128),
    db: AsyncSession = Depends(get_db),
):
    """处理 OIDC 回调 - 重定向到前端 Vue 路由"""
    return await oidc_callback_handler(code, state, request, db, error)


@auth.post("/oidc/exchange-code", response_model=Token)
async def oidc_exchange_code(
    request: Request,
    response: Response,
    code: str = Body(..., embed=True, min_length=1, max_length=128),
    db: AsyncSession = Depends(get_db),
):
    """使用一次性 code 交换 OIDC 登录数据"""
    return await oidc_exchange_code_handler(code, request, response, db)


@auth.post("/oidc/logout")
async def oidc_logout(request: Request, response: Response, user=Depends(get_required_user), db=Depends(get_db)):
    """当前用户只撤销 Yuxi 本地凭据，保留认证中心共享会话。"""
    response.headers["Cache-Control"] = "no-store"
    return await oidc_logout_handler(request, db)


@auth.post("/oidc/backchannel-logout")
async def oidc_backchannel_logout(logout_token: str = Form(..., max_length=16384), db=Depends(get_db)):
    """接收认证中心签名的标准登出通知。"""
    from yuxi.modules.identity.services.oidc import OIDCService
    from yuxi.shared.errors import DomainError

    try:
        await OIDCService(db).backchannel_logout(logout_token)
    except DomainError as exc:
        raise HTTPException(400, "OIDC 登出通知无效") from exc
    return Response(status_code=200, headers={"Cache-Control": "no-store"})


def _raise_cli_auth_error(exc: CLIAuthError) -> None:
    """把 CLI 授权失败映射为携带错误码的 HTTP 响应。"""
    raise HTTPException(status_code=exc.status_code, detail={"error": exc.code, "message": exc.message}) from exc


class OASSOTokenRequest(BaseModel):
    """OA 页面下发的现有登录凭证。"""

    token: str = Field(min_length=1, max_length=MAX_OA_TOKEN_LENGTH)


class OAAccountLoginRequest(BaseModel):
    """父项目内嵌时下发的 OA 账号。"""

    account: str = Field(min_length=1, max_length=MAX_OA_ACCOUNT_LENGTH)


@auth.post("/oa/exchange-token", response_model=Token)
async def exchange_oa_token(data: OASSOTokenRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """验证 OA 现有 token 并签发 Yuxi 登录凭证。"""
    return await exchange_oa_token_handler(data.token, db, ip_address=extract_client_ip(request) if request else None)


@auth.post("/oa/exchange-account", response_model=Token)
async def exchange_oa_account(data: OAAccountLoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """使用父项目提供的 OA 账号换取 Yuxi 登录凭证。"""
    return await exchange_oa_account_handler(data.account, db, ip_address=extract_client_ip(request) if request else None)


@auth.get("/me", response_model=CurrentUserResponse)
async def read_users_me(authorization: AuthorizationContext = Depends(get_authorization_context), db: AsyncSession = Depends(get_db)):
    """获取当前登录用户的个人信息"""
    return await enterprise_management.read_users_me(authorization=authorization, db=db)


@auth.put("/profile", response_model=UserResponse)
async def update_profile(
    profile_data: UserProfileUpdate, request: Request, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)
):
    """更新当前用户的个人资料"""
    return await enterprise_management.update_profile(
        profile_data=profile_data, ip_address=extract_client_ip(request) if request else None, current_user=current_user, db=db
    )


@auth.put("/password", response_model=dict)
async def change_own_password(
    password_data: UserPasswordChange, request: Request, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)
):
    """校验原密码后更新当前用户的登录密码。

    原密码校验是安全底线：令牌泄露时，攻击者不能直接改密接管账号。
    改密成功后清零登录失败计数与锁定状态，避免旧锁定继续挡住新密码登录；
    并写入「密码已更新」操作日志——该文案同时是批量运维判断「密码是否被改过」的判据。
    """
    return await enterprise_management.change_own_password(
        password_data=password_data, ip_address=extract_client_ip(request) if request else None, current_user=current_user, db=db
    )


@auth.post("/users", response_model=UserResponse)
async def create_user(
    user_data: UserCreate,
    request: Request,
    authorization: AuthorizationContext = Depends(require_permission("user:create")),
    db: AsyncSession = Depends(get_db),
):
    """在当前管理域内创建新用户。"""
    return await enterprise_management.create_user(
        user_data=user_data, ip_address=extract_client_ip(request) if request else None, authorization=authorization, db=db
    )


@auth.get("/users/page", response_model=UserPageResponse)
async def read_users_page(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    search: str | None = Query(None, max_length=100),
    department_id: int | None = Query(None, ge=1),
    department_ids: str | None = Query(None, max_length=1000),
    role: str | None = Query(None, max_length=64),
    roles: str | None = Query(None, max_length=1000),
    user_kind: Literal["human", "end_user", "all"] = "human",
    authorization: AuthorizationContext = Depends(require_permission("user:read")),
    db: AsyncSession = Depends(get_db),
):
    """分页返回真人及其 owner 管理域内的 APP 身份。"""
    return await enterprise_management.read_users_page(
        offset=offset,
        limit=limit,
        search=search,
        department_id=department_id,
        department_ids=parse_csv_ints(department_ids),
        role=role,
        roles=parse_csv_roles(roles),
        user_kind=user_kind,
        authorization=authorization,
        db=db,
    )


@auth.get("/users", response_model=list[UserResponse])
async def read_users(
    response: Response,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    department_id: int | None = Query(None, ge=1),
    department_ids: str | None = Query(None, max_length=1000),
    direct: bool = False,
    keyword: str | None = Query(None, max_length=100),
    role: str | None = Query(None, max_length=64),
    roles: str | None = Query(None, max_length=1000),
    user_kind: Literal["human", "end_user", "all"] = "human",
    authorization: AuthorizationContext = Depends(require_permission("user:read")),
    db: AsyncSession = Depends(get_db),
):
    """返回管理域内用户；组织筛选默认包含全部后代。"""
    rows, total = await enterprise_management.read_users(
        skip=skip,
        limit=limit,
        department_id=department_id,
        department_ids=parse_csv_ints(department_ids),
        direct=direct,
        keyword=keyword,
        role=role,
        roles=parse_csv_roles(roles),
        user_kind=user_kind,
        authorization=authorization,
        db=db,
    )
    response.headers["X-Total-Count"] = str(total)
    return rows


@auth.get("/users/access-options", response_model=list[UserAccessOption])
async def read_user_access_options(
    response: Response,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    keyword: str | None = Query(None, max_length=100),
    department_id: int | None = Query(None, ge=1),
    direct: bool = False,
    authorization: AuthorizationContext = Depends(require_permission("user:read")),
    db: AsyncSession = Depends(get_db),
):
    """返回管理域内的候选用户，支持关键字远程检索。

    授权范围过滤与关键字筛选均在仓储层以 SQL 完成，避免把全量用户
    载入内存后逐个判定；响应头回传 X-Total-Count 供调用方判断是否还有更多。
    """
    rows, total = await enterprise_management.read_user_access_options(
        skip=skip, limit=limit, keyword=keyword, department_id=department_id, direct=direct, authorization=authorization, db=db
    )
    response.headers["X-Total-Count"] = str(total)
    return rows


@auth.get("/users/{user_id}", response_model=UserResponse)
async def read_user(
    user_id: int, authorization: AuthorizationContext = Depends(require_permission("user:read")), db: AsyncSession = Depends(get_db)
):
    """读取管理域内单个用户。"""
    return await enterprise_management.read_user(user_id=user_id, authorization=authorization, db=db)


@auth.put("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    user_data: UserUpdate,
    request: Request,
    authorization: AuthorizationContext = Depends(get_authorization_context),
    db: AsyncSession = Depends(get_db),
):
    """修改管理域内用户，并限制新的组织归属。"""
    return await enterprise_management.update_user(
        user_id=user_id, user_data=user_data, ip_address=extract_client_ip(request) if request else None, authorization=authorization, db=db
    )


@auth.delete("/users/{user_id}", response_model=dict)
async def delete_user(
    user_id: int,
    request: Request,
    authorization: AuthorizationContext = Depends(require_permission("user:delete")),
    db: AsyncSession = Depends(get_db),
):
    """软删除管理域内用户。"""
    return await enterprise_management.delete_user(
        user_id=user_id, ip_address=extract_client_ip(request) if request else None, authorization=authorization, db=db
    )


@auth.post("/validate-username", response_model=UidGeneration)
async def validate_username_and_generate_uid(
    validation_data: UsernameValidation,
    _authorization: AuthorizationContext = Depends(require_permission("user:create")),
    db: AsyncSession = Depends(get_db),
):
    """验证用户名格式并生成可用的user_id"""
    return await enterprise_management.validate_username_and_generate_uid(
        validation_data=validation_data, _authorization=_authorization, db=db
    )


@auth.get("/check-uid/{uid}")
async def check_uid_availability(
    uid: str, _authorization: AuthorizationContext = Depends(require_permission("user:create")), db: AsyncSession = Depends(get_db)
):
    """检查 uid 是否可用"""
    return await enterprise_management.check_uid_availability(uid=uid, _authorization=_authorization, db=db)


@auth.post("/impersonate/{user_id}", response_model=Token)
async def impersonate_user(
    user_id: int,
    request: Request,
    authorization: AuthorizationContext = Depends(require_permission("user:impersonate")),
    db: AsyncSession = Depends(get_db),
):
    """在当前管理域内模拟其他用户登录。"""
    return await enterprise_management.impersonate_user(
        user_id=user_id, ip_address=extract_client_ip(request) if request else None, authorization=authorization, db=db
    )
