"""用户与组织管理域查询。"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.permissions.authorization import AuthorizationContext, AuthorizationTarget, parse_department_ancestor_ids
from yuxi.repositories.department_repository import DepartmentRepository
from yuxi.repositories.user_repository import UserRepository
from yuxi.storage.postgres.models_business import User


def _user_target(user: User) -> AuthorizationTarget:
    """把已加载组织祖先链的用户转换为授权目标。"""

    ancestor_ids = getattr(user, "department_ancestor_ids", None)
    if ancestor_ids is None:
        raise ValueError(f"用户 {user.uid} 缺少组织授权上下文")
    return AuthorizationTarget(owner_user_id=user.id, department_ancestor_ids=tuple(ancestor_ids))


async def get_authorized_user(
    db: AsyncSession,
    authorization: AuthorizationContext,
    permission_key: str,
    user_id: int,
) -> User | None:
    """读取存在且位于当前权限管理域内的有效用户。"""

    user = await UserRepository().get_by_id_with_db(db, user_id)
    if user is None or user.is_deleted or not authorization.allows(permission_key, _user_target(user)):
        return None
    return user


async def department_is_accessible(
    authorization: AuthorizationContext,
    permission_key: str,
    department_id: int,
    *,
    db: AsyncSession | None = None,
) -> bool:
    """判断组织节点是否存在并位于当前权限的数据范围内。"""

    path = (await DepartmentRepository().get_paths_by_ids([department_id], session=db)).get(department_id)
    if not path:
        return False
    return authorization.allows(
        permission_key,
        AuthorizationTarget(
            department_ancestor_ids=parse_department_ancestor_ids(path),
        ),
    )


async def list_authorized_users(
    authorization: AuthorizationContext,
    permission_key: str,
    *,
    department_id: int | None = None,
    direct: bool = False,
    db: AsyncSession | None = None,
) -> list[tuple[User, str | None]] | None:
    """按同一管理域规则过滤用户，并区分子树和直属查询。"""

    if department_id is not None and not await department_is_accessible(
        authorization,
        permission_key,
        department_id,
        db=db,
    ):
        return None

    rows = await UserRepository().list_with_department(limit=None, session=db)
    visible_rows = []
    for user, department_name in rows:
        target = _user_target(user)
        if not authorization.allows(permission_key, target):
            continue
        if department_id is not None:
            matches_department = (
                user.department_id == department_id if direct else department_id in target.department_ancestor_ids
            )
            if not matches_department:
                continue
        visible_rows.append((user, department_name))
    return visible_rows


async def list_authorized_departments(
    authorization: AuthorizationContext,
    permission_key: str = "department:read",
    *,
    db: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """返回完整集团目录或当前授权子树及必要祖先。"""

    repository = DepartmentRepository()
    departments = await repository.list_with_user_count(session=db)
    if authorization.has_permission("department:read_all"):
        return departments

    paths = await repository.get_paths_by_ids([item["id"] for item in departments], session=db)
    visible_ids = {
        item["id"]
        for item in departments
        if authorization.allows(
            permission_key,
            AuthorizationTarget(department_ancestor_ids=parse_department_ancestor_ids(paths.get(item["id"]))),
        )
    }
    context_ids = {
        ancestor_id
        for department_id in visible_ids
        for ancestor_id in parse_department_ancestor_ids(paths.get(department_id))
    }
    return [
        {**item, "user_count": item["user_count"] if item["id"] in visible_ids else 0}
        for item in departments
        if item["id"] in context_ids
    ]
