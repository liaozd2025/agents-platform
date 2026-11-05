"""企业授权与新版 APP 身份共存时的映射及会话边界。"""

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from test.support.sqlite import create_utc_sqlite_engine
from yuxi.infrastructure.postgres.base import BusinessBase
from yuxi.modules.identity.models import Department, Role, RolePermission, User, UserRoleAssignment
from yuxi.modules.identity.permissions import build_authorization_context
from yuxi.modules.identity.repositories.roles import RoleRepository
from yuxi.modules.identity.repositories.users import UserRepository
from yuxi.modules.identity.services.login import login_result
from yuxi.modules.identity.services.role_assignments import UserRoleAuthorizationError, replace_user_role_assignments

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture
async def identity_session():
    """从数据库重新加载身份，避免构造时已加载的关系掩盖异步读取缺陷。"""
    engine = create_utc_sqlite_engine()
    async with engine.begin() as connection:
        await connection.run_sync(BusinessBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        department = Department(id=1, name="集团", path="/1/")
        role = Role(
            code="superadmin",
            name="超级管理员",
            is_active=True,
            is_builtin=True,
            default_scope_type="all",
            permissions=[RolePermission(permission_key="user:role_assign")],
            default_departments=[],
        )
        actor = User(
            username="管理员",
            uid="human-owner",
            password_hash="!disabled",
            user_kind="human",
            department=department,
            role_assignments=[UserRoleAssignment(role=role, scope_mode="inherit", scope_departments=[])],
        )
        visitor = User(
            username="visitor",
            uid="app-visitor",
            password_hash="!disabled",
            user_kind="end_user",
            owner_user=actor,
            app_id="app",
            end_user_id="visitor",
            role_assignments=[],
        )
        db.add_all([actor, visitor])
        await db.commit()
        ids = actor.id, visitor.id, role.id
        db.expunge_all()
        yield db, ids
    await engine.dispose()


async def test_password_lookup_contains_serializable_role_and_scope_snapshot(identity_session):
    """密码查询结果无需隐式 IO 即可生成完整登录响应。"""
    db, _ids = identity_session
    user = await UserRepository(db).get_by_login_identifier("human-owner")
    result = login_result(user, "test-token")
    assert result["roles"][0]["code"] == "superadmin"
    assert result["roles"][0]["effective_scope_type"] == "all"
    assert "user:role_assign" in result["effective_permissions"]


async def test_identity_row_lock_preserves_loaded_authorization(identity_session):
    """取得身份共享或独占锁后，授权投影仍可直接使用。"""
    db, ids = identity_session
    repo = UserRepository(db)
    user = await repo.get_by_id(ids[0])
    assert build_authorization_context(user).has_permission("user:role_assign")
    locked = await repo.lock_active_human(user.uid)
    assert locked is user
    assert build_authorization_context(locked).has_permission("user:role_assign")
    assert locked.department_ancestor_ids == (1,)
    locked = await repo.get_active_by_id(user.id, for_update=True)
    assert build_authorization_context(locked).has_permission("user:role_assign")


async def test_app_identity_read_keeps_owner_authorization_loaded(identity_session):
    """读取 APP 身份不会卸载同一会话中操作人的角色集合。"""
    db, ids = identity_session
    repo = UserRepository(db)
    actor = await repo.get_by_id(ids[0])
    visitor = await repo.get_by_id(ids[1])
    assert visitor.owner_user is actor
    assert actor.department_ancestor_ids == (1,)
    assert build_authorization_context(actor).has_permission("user:role_assign")
    assert not build_authorization_context(visitor).has_permission("user:role_assign")


async def test_app_identity_cannot_receive_enterprise_roles(identity_session):
    """直接调用角色写入 Owner 也拒绝 APP 终端身份。"""
    db, ids = identity_session
    repo = UserRepository(db)
    actor = await repo.get_by_id(ids[0])
    visitor = await repo.get_by_id(ids[1])
    with pytest.raises(UserRoleAuthorizationError, match="APP"):
        await replace_user_role_assignments(
            db,
            authorization=build_authorization_context(actor),
            target=visitor,
            assignments=[{"role_id": ids[2]}],
            reason="测试拒绝",
        )
    assert await db.scalar(select(func.count(UserRoleAssignment.id)).where(UserRoleAssignment.user_id == ids[1])) == 0


async def test_invalid_shadow_assignment_does_not_replace_last_human_admin(identity_session):
    """异常历史分配不能替代最后一位真人管理员。"""
    db, ids = identity_session
    db.add(UserRoleAssignment(user_id=ids[1], role_id=ids[2], scope_mode="inherit"))
    await db.commit()
    assert await RoleRepository(db).lock_superadmin_and_count_active_users() == 1
    user = await UserRepository(db).get_by_id(ids[0])
    with pytest.raises(ValueError, match="保留"):
        await UserRepository(db).delete_for_admin(user)
    await db.rollback()
    assert await db.scalar(select(User.is_deleted).where(User.id == ids[0])) == 0
    visitor = await UserRepository(db).get_by_id(ids[1])
    assert not build_authorization_context(visitor).has_permission("user:role_assign")


async def test_builtin_role_sync_preserves_assignments_and_is_repeatable(identity_session):
    """内置目录同步保留角色主键及已有分配，不重复插入权限。"""
    from yuxi.bootstrap.identity import ensure_builtin_roles
    from yuxi.modules.identity.permissions.role_catalog import BUILTIN_ROLES

    db, ids = identity_session
    custom = Role(code="custom-audit", name="自定义", default_scope_type="self", is_builtin=False)
    db.add(custom)
    await db.flush()
    await ensure_builtin_roles(db)
    await ensure_builtin_roles(db)
    assert await db.scalar(select(func.count(Role.id))) == len(BUILTIN_ROLES) + 1
    assert await db.scalar(select(func.count(UserRoleAssignment.id)).where(UserRoleAssignment.user_id == ids[0])) == 1
    assert await db.scalar(select(Role.id).where(Role.code == "superadmin")) == ids[2]
    assert await db.scalar(select(Role.name).where(Role.id == custom.id)) == "自定义"
    assert await db.scalar(select(func.count(RolePermission.permission_key))) == sum(len(role.permission_keys) for role in BUILTIN_ROLES)


async def test_fresh_admin_initialization_returns_complete_enterprise_login_identity():
    """初始化事务返回集团根、多角色身份与可直接使用的登录投影。"""
    from yuxi.bootstrap.identity import ensure_builtin_roles
    from yuxi.modules.identity.services.administration import initialize_system_admin

    engine = create_utc_sqlite_engine()
    async with engine.begin() as connection:
        await connection.run_sync(BusinessBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        await ensure_builtin_roles(db)
        await db.commit()
        created = await initialize_system_admin(db, uid="initial-admin", password="test-initial-password", phone_number=None)
        result = login_result(created.admin, "test-token")
        assert created.department.path == "/1/"
        assert result["roles"][0]["code"] == "superadmin"
        assert "user:role_assign" in result["effective_permissions"]
        assert await db.scalar(select(func.count(UserRoleAssignment.id))) == 1
    await engine.dispose()


async def test_department_service_keeps_tree_names_and_rejects_parent_deletion(identity_session):
    """组织接口用例保留跨父节点同名，并拒绝删除仍有子节点的组织。"""
    from yuxi.bootstrap.identity import ensure_builtin_roles
    from yuxi.modules.identity.services import departments
    from yuxi.shared.errors import InvalidOperationError

    db, ids = identity_session
    await ensure_builtin_roles(db)
    actor = await UserRepository(db).get_by_id(ids[0])
    authorization = build_authorization_context(actor)
    parent = await departments.create_department(db, authorization=authorization, ip_address=None, name="研发")
    child = await departments.create_department(db, authorization=authorization, ip_address=None, name="研发", parent_id=parent["id"])
    assert await db.scalar(select(Department.path).where(Department.id == child["id"])) == f"/1/{parent['id']}/{child['id']}/"
    with pytest.raises(InvalidOperationError, match="同名"):
        await departments.create_department(db, authorization=authorization, ip_address=None, name="研发")
    with pytest.raises(InvalidOperationError, match="子节点"):
        await departments.delete_department(db, department_id=parent["id"], authorization=authorization, ip_address=None)
    assert await db.scalar(select(Department.id).where(Department.id == parent["id"])) == parent["id"]
    result = await departments.delete_department(db, department_id=child["id"], authorization=authorization, ip_address=None)
    assert result["success"] is True
    assert await db.scalar(select(Department.id).where(Department.id == child["id"])) is None
