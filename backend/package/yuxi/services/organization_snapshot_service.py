"""历史事件的用户组织快照。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import Department, User


async def get_user_organization_snapshot(
    db: AsyncSession,
    *,
    user_id: int | None = None,
    uid: str | None = None,
    inferred: bool = False,
) -> dict:
    """读取用户当前组织关系，生成可直接写入事件模型的快照字段。"""

    if user_id is None and uid is None:
        return {
            "organization_id_snapshot": None,
            "organization_path_snapshot": None,
            "organization_snapshot_inferred": inferred,
        }

    criterion = User.id == user_id if user_id is not None else User.uid == str(uid)
    result = await db.execute(
        select(User.department_id, Department.path)
        .outerjoin(Department, User.department_id == Department.id)
        .where(criterion)
    )
    row = result.one_or_none()
    if row is None:
        raise ValueError("组织快照对应的用户不存在")
    if row.department_id is not None and not row.path:
        raise ValueError("组织快照对应的组织路径无效")
    return {
        "organization_id_snapshot": row.department_id,
        "organization_path_snapshot": row.path,
        "organization_snapshot_inferred": inferred,
    }
