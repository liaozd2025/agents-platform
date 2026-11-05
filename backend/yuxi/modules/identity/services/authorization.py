"""身份写入前的事务锁与授权快照刷新。"""

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.modules.identity.permissions import AuthorizationContext, build_authorization_context
from yuxi.modules.identity.repositories.users import UserRepository
from yuxi.shared.errors import PermissionDeniedError


async def lock_authorization(db: AsyncSession, authorization: AuthorizationContext) -> AuthorizationContext:
    """在身份共同锁内刷新操作人的有效权限，避免等待期间撤权后仍写入。"""
    repository = UserRepository(db)
    await repository.lock_identity_changes()
    user = await repository.get_active_by_id(authorization.user.id, for_update=True)
    if user is None or user.user_kind != "human":
        raise PermissionDeniedError("当前身份不能管理组织")
    return build_authorization_context(user)
