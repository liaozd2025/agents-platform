"""用户偏好保存用例。"""

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.infrastructure.observability.logging import logger
from yuxi.modules.agents.services.user_memory import sync_user_profile_to_memory
from yuxi.modules.identity.preferences import UserConfig, UserConfigSchema
from yuxi.modules.identity.repositories.preferences import upsert_user_config


async def save_user_config(db: AsyncSession, *, uid: str, schema: UserConfigSchema) -> UserConfig:
    """提交用户配置，失败时不保留部分写入。"""
    try:
        updated_at = await upsert_user_config(db, uid=uid, enable_memory=schema.enable_memory)
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    try:
        await sync_user_profile_to_memory(db=db, uid=uid)
    except Exception:
        logger.bind(component="user_memory", uid=uid).exception("Memory 配置已保存，用户画像文件同步失败")
        raise
    return UserConfig(uid=uid, schema=schema, updated_at=updated_at)
