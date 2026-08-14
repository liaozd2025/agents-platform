"""将智能体功能权限校验适配为 FastAPI 依赖。"""

from fastapi import Depends

from server.utils.auth_middleware import require_permission
from yuxi.permissions.authorization import AuthorizationContext
from yuxi.storage.postgres.models_business import User


async def require_agent_use_permission(
    authorization: AuthorizationContext = Depends(require_permission("agent:use")),
) -> User:
    """校验智能体使用功能权限。"""

    return authorization.user
