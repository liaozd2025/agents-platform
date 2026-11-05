"""用户管理、资料与头像用例，事务在服务入口收敛。"""

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.modules.identity.models import User
from yuxi.modules.identity.repositories.users import UserRepository


async def upload_avatar(db: AsyncSession, *, current_user: User, content: bytes) -> str:
    """上传头像后提交用户引用；对象上传和数据库保持各自失败边界。"""
    from yuxi.infrastructure.minio import upload_image_to_minio

    try:
        avatar_url = await upload_image_to_minio(
            content, object_prefix=f"avatar/{current_user.id}", max_size_bytes=5 * 1024 * 1024, too_large_message="文件大小不能超过5MB"
        )
        current_user.avatar = avatar_url
        await UserRepository(db).save(current_user)
        await db.commit()
        return avatar_url
    except BaseException:
        await db.rollback()
        raise


async def is_first_run() -> bool:
    """系统是否尚未创建任何用户。"""
    return await UserRepository().is_first_run()
