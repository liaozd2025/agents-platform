"""用户与组织管理域查询。"""

from typing import Any

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.modules.identity.models import Department, User
from yuxi.modules.identity.permissions.authorization import AuthorizationContext, AuthorizationTarget, parse_department_ancestor_ids
from yuxi.modules.identity.repositories.departments import DepartmentRepository
from yuxi.modules.identity.repositories.users import UserRepository


def _user_target(user: User) -> AuthorizationTarget:
    """把已加载组织祖先链的用户转换为授权目标。"""

    ancestor_ids = getattr(user, "department_ancestor_ids", None)
    if ancestor_ids is None:
        raise ValueError(f"用户 {user.uid} 缺少组织授权上下文")
    return AuthorizationTarget(owner_user_id=user.id, department_ancestor_ids=tuple(ancestor_ids))


def managed_user_target(user: User) -> AuthorizationTarget | None:
    """真人按自身组织授权，APP 只按当前有效真人 owner 授权。"""
    if user.is_deleted:
        return None
    owner = user.owner_user if user.user_kind == "end_user" else user
    if owner is None or owner.is_deleted or owner.user_kind != "human":
        return None
    return _user_target(owner)


async def get_authorized_user(
    db: AsyncSession,
    authorization: AuthorizationContext,
    permission_key: str,
    user_id: int,
    *,
    include_app: bool = False,
) -> User | None:
    """默认仅管理真人；APP 阅读与删除入口显式选择 owner 范围。"""
    user = await UserRepository().get_by_id_with_db(db, user_id)
    if user is None or (user.user_kind != "human" and not include_app):
        return None
    target = managed_user_target(user)
    if target is None or not authorization.allows(permission_key, target):
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
            matches_department = user.department_id == department_id if direct else department_id in target.department_ancestor_ids
            if not matches_department:
                continue
        visible_rows.append((user, department_name))
    return visible_rows


async def list_authorized_users_page(
    authorization: AuthorizationContext,
    permission_key: str,
    *,
    skip: int = 0,
    limit: int = 100,
    department_id: int | None = None,
    department_ids: list[int] | None = None,
    direct: bool = False,
    keyword: str | None = None,
    role: str | None = None,
    roles: list[str] | None = None,
    user_kind: str = "human",
    db: AsyncSession | None = None,
) -> tuple[list[tuple[User, str | None]], int] | None:
    """把授权范围编译成 SQL，并在仓储层完成筛选、计数和分页。"""

    selected_departments = set(department_ids or []) | ({department_id} if department_id is not None else set())
    for selected_id in selected_departments:
        if not await department_is_accessible(authorization, permission_key, selected_id, db=db):
            return None

    clauses: list[Any] = []
    current_department_id = authorization.user.department_id
    for scope_type, selected_ids in authorization.permission_scopes(permission_key):
        if scope_type == "all":
            clauses.append(True)
        elif scope_type == "self":
            clauses.append(or_(User.id == authorization.user.id, User.owner_user_id == authorization.user.id))
        elif scope_type == "organization_and_descendants" and current_department_id is not None:
            clauses.append(Department.path.like(f"%/{current_department_id}/%"))
        elif scope_type == "selected_organizations_and_descendants":
            clauses.extend(Department.path.like(f"%/{item}/%") for item in selected_ids)

    return await UserRepository().list_with_department_page(
        visibility_clauses=tuple(clauses),
        department_id=department_id,
        department_ids=department_ids,
        direct=direct,
        keyword=keyword,
        role=role,
        roles=roles,
        user_kind=user_kind,
        skip=skip,
        limit=limit,
        session=db,
    )


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
    context_ids = {ancestor_id for department_id in visible_ids for ancestor_id in parse_department_ancestor_ids(paths.get(department_id))}
    return [
        {**item, "user_count": item["user_count"] if item["id"] in visible_ids else 0} for item in departments if item["id"] in context_ids
    ]
