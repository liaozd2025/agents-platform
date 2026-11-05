"""历史事件的用户组织快照。"""

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from yuxi.modules.identity.models import Department, User


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
    owner = aliased(User)
    effective_owner_id = case((User.user_kind == "end_user", User.owner_user_id), else_=User.id)
    result = await db.execute(
        select(
            owner.department_id,
            Department.path,
            User.is_deleted.label("actor_deleted"),
            owner.is_deleted.label("owner_deleted"),
            owner.user_kind.label("owner_kind"),
        )
        .select_from(User)
        .join(owner, owner.id == effective_owner_id)
        .outerjoin(Department, owner.department_id == Department.id)
        .where(criterion)
    )
    row = result.one_or_none()
    if row is None:
        raise ValueError("组织快照对应的用户不存在")
    if row.actor_deleted or row.owner_deleted or row.owner_kind != "human":
        raise ValueError("组织快照对应的用户或所属用户已失效")
    if row.department_id is not None and not row.path:
        raise ValueError("组织快照对应的组织路径无效")
    return {
        "organization_id_snapshot": row.department_id,
        "organization_path_snapshot": row.path,
        "organization_snapshot_inferred": inferred,
    }
