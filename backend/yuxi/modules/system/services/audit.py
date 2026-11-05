"""事务内审计事实，仅消费协议无关的用户与客户端地址。"""

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.modules.identity.services.organization_snapshot import get_user_organization_snapshot
from yuxi.modules.system.models import OperationLog


async def log_operation(
    db: AsyncSession,
    user_id: int | None,
    operation: str,
    details: str | None = None,
    *,
    ip_address: str | None = None,
) -> None:
    """把强制审计事实加入当前 owning transaction；失败必须阻止业务提交。"""

    snapshot = await get_user_organization_snapshot(db, user_id=user_id)
    db.add(
        OperationLog(
            user_id=user_id,
            operation=operation,
            details=details,
            ip_address=ip_address,
            **snapshot,
        )
    )
    await db.flush()
