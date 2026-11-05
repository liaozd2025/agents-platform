"""共享范围写入的身份校验。"""

from sqlalchemy import select

from yuxi.modules.identity.models import User
from yuxi.modules.identity.permissions import get_permission_department_ids, normalize_permission_config
from yuxi.modules.identity.repositories.departments import DepartmentRepository


async def validate_shared_grants(db, share_config: dict) -> dict:
    """校验组织子树和目标身份；操作功能权限由实际用例单独求交。"""
    paths = await DepartmentRepository(db).get_paths_by_ids(get_permission_department_ids(share_config), session=db)
    config = normalize_permission_config(share_config, strict=True, department_paths=paths)
    scope = config["manage_scope"]
    if scope and scope["access_level"] == "user":
        result = await db.execute(
            select(User.uid)
            .where(
                User.uid.in_(scope["user_uids"]),
                User.is_deleted == 0,
                User.user_kind == "human",
            )
            .with_for_update(read=True)
        )
        if set(result.scalars()) != set(scope["user_uids"]):
            raise ValueError("指定管理用户必须是有效企业用户")
    return config
