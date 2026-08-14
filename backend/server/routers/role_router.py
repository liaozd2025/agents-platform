"""角色与权限只读总览接口。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.services.role_service import get_role_overview
from yuxi.storage.postgres.models_business import User

from server.utils.auth_middleware import get_db, get_superadmin_user

roles = APIRouter(prefix="/roles", tags=["roles"])


@roles.get("/overview")
async def read_role_overview(
    _current_user: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """返回只允许超级管理员查看的角色、功能权限、范围和成员。"""

    return await get_role_overview(db)
