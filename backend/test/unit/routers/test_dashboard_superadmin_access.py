from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from types import SimpleNamespace

from server.routers.dashboard_router import (
    dashboard,
    get_all_conversations,
    get_conversation_detail,
    get_current_organization_stats,
    get_tool_call_stats,
    get_user_activity_stats,
)
from server.utils.auth_middleware import get_superadmin_user
from yuxi.permissions.authorization import build_authorization_context
from yuxi.storage.postgres.models_business import Base, Conversation, Department, Message, ToolCall, User
from yuxi.utils.datetime_utils import utc_now_naive

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture()
async def dashboard_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        root = Department(name="Group", node_type="group", path="/1/")
        db.add(root)
        await db.flush()
        dept_a = Department(name="Dept A", parent_id=root.id)
        dept_b = Department(name="Dept B", parent_id=root.id)
        db.add_all([dept_a, dept_b])
        await db.flush()
        dept_a.path = f"{root.path}{dept_a.id}/"
        dept_b.path = f"{root.path}{dept_b.id}/"
        child_a = Department(name="Child A", parent_id=dept_a.id)
        db.add(child_a)
        await db.flush()
        child_a.path = f"{dept_a.path}{child_a.id}/"
        superadmin = User(
            username="Super Admin",
            uid="superadmin",
            password_hash="$argon2id$placeholder",
            role="superadmin",
            department=dept_a,
        )
        admin_a = User(
            username="Admin A",
            uid="admin_a",
            password_hash="$argon2id$placeholder",
            role="admin",
            department=dept_a,
        )
        user_a = User(
            username="User A",
            uid="user_a",
            password_hash="$argon2id$placeholder",
            role="user",
            department=dept_a,
        )
        admin_b = User(
            username="Admin B",
            uid="admin_b",
            password_hash="$argon2id$placeholder",
            role="admin",
            department=dept_b,
        )
        user_b = User(
            username="User B",
            uid="user_b",
            password_hash="$argon2id$placeholder",
            role="user",
            department=dept_b,
        )
        child_user = User(
            username="Child User",
            uid="child_user",
            password_hash="$argon2id$placeholder",
            role="user",
            department=child_a,
        )
        now = utc_now_naive()
        conversation_a = Conversation(
            thread_id="thread-a",
            uid="user_a",
            agent_id="agent-shared",
            title="Dept A conversation",
            status="active",
            created_at=now,
            updated_at=now,
            organization_id_snapshot=dept_a.id,
            organization_path_snapshot=dept_a.path,
        )
        conversation_b = Conversation(
            thread_id="thread-b",
            uid="user_b",
            agent_id="agent-shared",
            title="Dept B conversation",
            status="active",
            created_at=now,
            updated_at=now,
            organization_id_snapshot=dept_b.id,
            organization_path_snapshot=dept_b.path,
        )
        message_a = Message(conversation=conversation_a, role="assistant", content="A", created_at=now)
        message_b = Message(conversation=conversation_b, role="assistant", content="B", created_at=now)
        tool_call_a = ToolCall(
            message=message_a,
            tool_name="dept_a_tool",
            status="success",
            created_at=now,
            organization_id_snapshot=dept_a.id,
            organization_path_snapshot=dept_a.path,
        )
        tool_call_b = ToolCall(
            message=message_b,
            tool_name="dept_b_tool",
            status="success",
            created_at=now,
            organization_id_snapshot=dept_b.id,
            organization_path_snapshot=dept_b.path,
        )
        db.add_all(
            [
                dept_a,
                dept_b,
                superadmin,
                admin_a,
                user_a,
                admin_b,
                user_b,
                child_user,
                conversation_a,
                conversation_b,
                message_a,
                message_b,
                tool_call_a,
                tool_call_b,
            ]
        )
        await db.commit()
        for item in [
            dept_a,
            dept_b,
            child_a,
            superadmin,
            admin_a,
            user_a,
            admin_b,
            user_b,
            child_user,
            conversation_a,
            conversation_b,
        ]:
            await db.refresh(item)
        yield {
            "db": db,
            "superadmin": superadmin,
            "admin_a": admin_a,
            "user_a": user_a,
            "dept_a": dept_a,
            "dept_b": dept_b,
            "child_a": child_a,
        }
    await engine.dispose()


async def test_dashboard_current_organization_uses_permission_dependency():
    dashboard_routes = [route for route in dashboard.routes if isinstance(route, APIRoute)]

    assert dashboard_routes
    for route in dashboard_routes:
        dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
        assert get_superadmin_user not in dependency_calls
        assert any(call.__name__ == "check_permission" for call in dependency_calls)


async def test_dashboard_dependency_rejects_department_admin(dashboard_session):
    with pytest.raises(HTTPException) as exc:
        await get_superadmin_user(dashboard_session["admin_a"])

    assert exc.value.status_code == 403


async def test_conversation_list_superadmin_sees_all_departments(dashboard_session):
    authorization = _authorization(dashboard_session["superadmin"], ["dashboard:view"], "all")
    response = await get_all_conversations(db=dashboard_session["db"], authorization=authorization)

    assert {item["thread_id"] for item in response} == {"thread-a", "thread-b"}


async def test_conversation_detail_superadmin_can_view_other_department(dashboard_session):
    authorization = _authorization(dashboard_session["superadmin"], ["dashboard:view"], "all")
    response = await get_conversation_detail(
        "thread-b",
        db=dashboard_session["db"],
        authorization=authorization,
    )

    assert response["thread_id"] == "thread-b"


async def test_user_activity_stats_superadmin_include_all_departments(dashboard_session):
    authorization = _authorization(dashboard_session["superadmin"], ["dashboard:view"], "all")
    stats = await get_user_activity_stats(db=dashboard_session["db"], authorization=authorization)

    assert stats.total_users == 6
    assert stats.active_users_24h == 2
    assert stats.active_users_30d == 2


async def test_tool_stats_superadmin_include_all_departments(dashboard_session):
    authorization = _authorization(dashboard_session["superadmin"], ["dashboard:view"], "all")
    stats = await get_tool_call_stats(db=dashboard_session["db"], authorization=authorization)

    assert stats.total_calls == 2
    assert stats.successful_calls == 2
    assert {tool["tool_name"] for tool in stats.most_used_tools} == {"dept_a_tool", "dept_b_tool"}


def _authorization(user, permission_keys, scope_type):
    """构造 Dashboard 数据范围测试所需的最小授权上下文。"""

    role = SimpleNamespace(
        is_active=True,
        permissions=[SimpleNamespace(permission_key=key) for key in permission_keys],
        default_scope_type=scope_type,
        default_departments=[],
    )
    assignment = SimpleNamespace(role=role, scope_mode="inherit", scope_departments=[])
    authorization_user = SimpleNamespace(
        id=user.id,
        uid=user.uid,
        department_id=user.department_id,
        role_assignments=[assignment],
    )
    return build_authorization_context(authorization_user)


async def test_current_organization_stats_include_authorized_descendants(dashboard_session):
    authorization = _authorization(
        dashboard_session["admin_a"],
        ["dashboard:view"],
        "organization_and_descendants",
    )

    all_stats = await get_current_organization_stats(
        authorization=authorization,
        db=dashboard_session["db"],
    )
    child_stats = await get_current_organization_stats(
        department_id=dashboard_session["child_a"].id,
        authorization=authorization,
        db=dashboard_session["db"],
    )

    assert all_stats.total_users == 4
    assert all_stats.total_departments == 2
    assert {item.id for item in all_stats.departments} == {
        dashboard_session["dept_a"].parent_id,
        dashboard_session["dept_a"].id,
        dashboard_session["child_a"].id,
    }
    root_option = next(item for item in all_stats.departments if item.id == dashboard_session["dept_a"].parent_id)
    assert root_option.selectable is False
    assert child_stats.total_users == 1
    assert child_stats.total_departments == 1
    assert child_stats.selected_department_name == "Child A"
    assert child_stats.includes_descendants is True


async def test_current_organization_stats_hide_out_of_scope_target(dashboard_session):
    authorization = _authorization(
        dashboard_session["admin_a"],
        ["dashboard:view"],
        "organization_and_descendants",
    )

    with pytest.raises(HTTPException) as exc:
        await get_current_organization_stats(
            department_id=dashboard_session["dept_b"].id,
            authorization=authorization,
            db=dashboard_session["db"],
        )

    assert exc.value.status_code == 404


async def test_historical_tool_stats_keep_event_organization_after_user_move(dashboard_session):
    authorization = _authorization(
        dashboard_session["admin_a"],
        ["dashboard:view"],
        "organization_and_descendants",
    )
    dashboard_session["user_a"].department_id = dashboard_session["dept_b"].id
    await dashboard_session["db"].flush()

    stats = await get_tool_call_stats(
        department_id=dashboard_session["dept_a"].id,
        authorization=authorization,
        db=dashboard_session["db"],
    )

    assert stats.total_calls == 1
    assert stats.most_used_tools == [{"tool_name": "dept_a_tool", "count": 1}]
