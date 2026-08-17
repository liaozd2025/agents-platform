"""用户级配置与凭据路由"""

import re
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_authorization_context, get_current_user, get_db, get_required_user
from yuxi.config import UserConfig, UserConfigSchema
from yuxi.permissions.authorization import AuthorizationContext
from yuxi.services.user_management_service import get_authorized_user, list_authorized_users
from yuxi.storage.minio import upload_image_to_minio
from yuxi.storage.postgres.models_business import APIKey, AgentEnv, User
from yuxi.utils.auth_utils import AuthUtils
from yuxi.utils.datetime_utils import coerce_any_to_utc_datetime, format_utc_datetime, utc_now_naive

user_router = APIRouter(prefix="/user", tags=["user"])

ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_ENV_COUNT = 200
MAX_ENV_KEY_LENGTH = 128
MAX_ENV_VALUE_LENGTH = 32768
MAX_USER_IMAGE_SIZE_BYTES = 5 * 1024 * 1024


class APIKeyCreate(BaseModel):
    name: str
    user_id: int | None = None
    department_id: int | None = None
    expires_at: str | None = None


class APIKeyUpdate(BaseModel):
    name: str | None = None
    expires_at: str | None = None
    is_enabled: bool | None = None


class APIKeyResponse(BaseModel):
    id: int
    key_prefix: str
    name: str
    user_id: int
    department_id: int | None
    expires_at: str | None
    is_enabled: bool
    last_used_at: str | None
    created_by: str
    created_at: str


class APIKeyCreateResponse(BaseModel):
    api_key: APIKeyResponse
    secret: str


class AgentEnvUpdate(BaseModel):
    env: dict[str, Any] = Field(default_factory=dict)


class AgentEnvResponse(BaseModel):
    env: dict[str, str]
    updated_at: str | None = None


async def get_logged_in_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请登录后再访问",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


@user_router.get("/config", response_model=dict)
async def get_user_config(
    current_user: User = Depends(get_logged_in_user),
    db: AsyncSession = Depends(get_db),
):
    user_config = await UserConfig.load(db, current_user.uid)
    return user_config.dump_config()


@user_router.put("/config", response_model=dict)
async def update_user_config(
    data: UserConfigSchema,
    current_user: User = Depends(get_logged_in_user),
    db: AsyncSession = Depends(get_db),
):
    user_config = await UserConfig(uid=current_user.uid, schema=data).save(db)
    return user_config.dump_config()


@user_router.post("/upload-image", response_model=dict)
async def upload_user_image(file: UploadFile = File(...), current_user: User = Depends(get_required_user)):
    try:
        image_url = await upload_image_to_minio(
            file,
            object_prefix=f"images/{current_user.uid}",
            max_size_bytes=MAX_USER_IMAGE_SIZE_BYTES,
            too_large_message="图片大小不能超过 5MB",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return {"success": True, "image_url": image_url, "url": image_url}


def validate_agent_env(env: dict[str, Any]) -> dict[str, str]:
    if len(env) > MAX_ENV_COUNT:
        raise HTTPException(status_code=400, detail=f"环境变量数量不能超过 {MAX_ENV_COUNT} 个")

    normalized: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str):
            raise HTTPException(status_code=400, detail="环境变量名必须是字符串")
        name = key.strip()
        if not name:
            raise HTTPException(status_code=400, detail="环境变量名不能为空")
        if len(name) > MAX_ENV_KEY_LENGTH:
            raise HTTPException(status_code=400, detail=f"环境变量名长度不能超过 {MAX_ENV_KEY_LENGTH}")
        if not ENV_KEY_PATTERN.match(name):
            raise HTTPException(status_code=400, detail=f"环境变量名 {name} 格式不正确")
        if name in normalized:
            raise HTTPException(status_code=400, detail=f"环境变量名 {name} 重复")
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail=f"环境变量 {name} 的值必须是字符串")
        if len(value) > MAX_ENV_VALUE_LENGTH:
            raise HTTPException(status_code=400, detail=f"环境变量 {name} 的值过长")
        normalized[name] = value
    return normalized


async def get_accessible_api_key(
    db: AsyncSession,
    api_key_id: int,
    authorization: AuthorizationContext,
) -> APIKey:
    """返回本人或跨用户管理域内的 API Key。"""

    result = await db.execute(select(APIKey).filter(APIKey.id == api_key_id))
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    if api_key.user_id != authorization.user.id:
        if not authorization.has_permission("api_key:manage_all"):
            raise HTTPException(status_code=403, detail="无权操作此 API Key")
        if await get_authorized_user(db, authorization, "api_key:manage_all", api_key.user_id) is None:
            raise HTTPException(status_code=404, detail="API Key 不存在")
    return api_key


@user_router.get("/apikey/", response_model=dict)
async def list_api_keys(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    authorization: AuthorizationContext = Depends(get_authorization_context),
    db: AsyncSession = Depends(get_db),
):
    visible_user_ids = {authorization.user.id}
    if authorization.has_permission("api_key:manage_all"):
        visible_user_ids.update(user.id for user, _ in await list_authorized_users(authorization, "api_key:manage_all"))

    visibility_filter = APIKey.user_id.in_(visible_user_ids)
    query = select(APIKey).where(visibility_filter).order_by(APIKey.created_at.desc()).offset(skip).limit(limit)
    count_query = select(func.count(APIKey.id)).where(visibility_filter)

    result = await db.execute(query)
    api_keys = result.scalars().all()
    total_result = await db.execute(count_query)

    return {
        "api_keys": [key.to_dict() for key in api_keys],
        "total": total_result.scalar(),
    }


@user_router.post("/apikey/", response_model=APIKeyCreateResponse)
async def create_api_key(
    data: APIKeyCreate,
    authorization: AuthorizationContext = Depends(get_authorization_context),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user

    target_user = current_user
    if data.user_id and data.user_id != current_user.id:
        if not authorization.has_permission("api_key:manage_all"):
            raise HTTPException(status_code=403, detail="无权为其他用户创建 API Key")
        user = await get_authorized_user(db, authorization, "api_key:manage_all", data.user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="关联的用户不存在")
        target_user = user

    if data.department_id is not None and data.department_id != target_user.department_id:
        raise HTTPException(status_code=403, detail="API Key 部门必须与关联用户部门一致")

    full_key, key_hash, key_prefix = AuthUtils.generate_api_key()
    expires_at = None
    if data.expires_at:
        aware_dt = coerce_any_to_utc_datetime(data.expires_at)
        if aware_dt:
            expires_at = aware_dt.replace(tzinfo=None)

    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=data.name,
        user_id=target_user.id,
        department_id=data.department_id,
        expires_at=expires_at,
        created_by=str(current_user.id),
    )

    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return APIKeyCreateResponse(
        api_key=APIKeyResponse(**api_key.to_dict()),
        secret=full_key,
    )


@user_router.get("/apikey/{api_key_id}", response_model=dict)
async def get_api_key(
    api_key_id: int,
    authorization: AuthorizationContext = Depends(get_authorization_context),
    db: AsyncSession = Depends(get_db),
):
    api_key = await get_accessible_api_key(db, api_key_id, authorization)
    return {"api_key": api_key.to_dict()}


@user_router.put("/apikey/{api_key_id}", response_model=dict)
async def update_api_key(
    api_key_id: int,
    data: APIKeyUpdate,
    authorization: AuthorizationContext = Depends(get_authorization_context),
    db: AsyncSession = Depends(get_db),
):
    api_key = await get_accessible_api_key(db, api_key_id, authorization)

    if data.name is not None:
        api_key.name = data.name
    if data.expires_at is not None:
        aware_dt = coerce_any_to_utc_datetime(data.expires_at)
        api_key.expires_at = aware_dt.replace(tzinfo=None) if aware_dt else None
    if data.is_enabled is not None:
        api_key.is_enabled = data.is_enabled

    await db.commit()
    await db.refresh(api_key)
    return {"api_key": api_key.to_dict()}


@user_router.delete("/apikey/{api_key_id}", response_model=dict)
async def delete_api_key(
    api_key_id: int,
    authorization: AuthorizationContext = Depends(get_authorization_context),
    db: AsyncSession = Depends(get_db),
):
    api_key = await get_accessible_api_key(db, api_key_id, authorization)

    await db.delete(api_key)
    await db.commit()
    return {"success": True}


@user_router.get("/agent-env", response_model=AgentEnvResponse)
async def get_agent_env(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AgentEnv).filter(AgentEnv.uid == current_user.uid))
    agent_env = result.scalar_one_or_none()
    if agent_env is None:
        return AgentEnvResponse(env={})
    return AgentEnvResponse(env=agent_env.env or {}, updated_at=format_utc_datetime(agent_env.updated_at))


@user_router.put("/agent-env", response_model=AgentEnvResponse)
async def update_agent_env(
    data: AgentEnvUpdate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    env = validate_agent_env(data.env)
    result = await db.execute(select(AgentEnv).filter(AgentEnv.uid == current_user.uid))
    current_agent_env = result.scalar_one_or_none()
    if current_agent_env is not None and (current_agent_env.env or {}) == env:
        return AgentEnvResponse(
            env=current_agent_env.env or {},
            updated_at=format_utc_datetime(current_agent_env.updated_at),
        )

    now = utc_now_naive()
    stmt = (
        pg_insert(AgentEnv)
        .values(uid=current_user.uid, env=env, updated_at=now)
        .on_conflict_do_update(
            index_elements=[AgentEnv.uid],
            set_={"env": env, "updated_at": now},
        )
        .returning(AgentEnv)
    )
    await db.execute(stmt)
    await db.commit()
    # 直接返回刚写入的 env/now，避免身份映射中的旧实例属性导致返回陈旧值
    return AgentEnvResponse(env=env, updated_at=format_utc_datetime(now))
