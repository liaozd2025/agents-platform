from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from yuxi.api.dependencies.auth import get_db, get_required_user
from yuxi.api.routers.extensions.skills import skills as system_skills_router
from yuxi.api.routers.extensions.skills import user_skills as user_skills_router
from yuxi.modules.extensions.skills.models import Skill
from yuxi.modules.identity.models import User, Role, RolePermission, UserRoleAssignment
from yuxi.modules.extensions.skills.package import SkillEditConflict


def _build_app(*, role: str = "admin", uid: str | None = None, permissions: set[str] | None = None) -> FastAPI:
    """真实权限依赖从角色快照求值，只替换已认证用户和数据库边界。"""
    app = FastAPI()
    app.include_router(system_skills_router, prefix="/api")
    app.include_router(user_skills_router, prefix="/api")
    keys = permissions if permissions is not None else ({"skill:manage", "skill:use"} if role in {"admin", "superadmin"} else {"skill:use"})
    assignment = Role(
        code=role,
        name=role,
        is_active=True,
        default_scope_type="all",
        default_departments=[],
        permissions=[RolePermission(permission_key=key) for key in keys],
    )
    user = User(
        id=1,
        username=uid or role,
        uid=uid or role,
        password_hash="x",
        department_id=1,
        is_deleted=0,
        user_kind="human",
        role_assignments=[UserRoleAssignment(role=assignment, scope_mode="inherit", scope_departments=[])],
    )
    user.department_ancestor_ids = (1,)
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_required_user] = lambda: user
    return app


def _skill_for_user(uid: str) -> Skill:
    """构造由共享权限逻辑投影管理权限的 Skill 数据。"""
    return Skill(
        slug="demo",
        name="demo",
        description="demo skill",
        source_type="upload",
        dir_path="shared/demo",
        share_config={
            "version": 2,
            "read_scope": {"access_level": "user", "user_uids": [uid]},
            "manage_scope": {"access_level": "user", "user_uids": [uid]},
        },
        enabled=True,
        created_by=uid,
        updated_by=uid,
    )


@pytest.mark.parametrize(
    ("role", "can_manage", "allowed_access_levels"),
    [
        ("admin", True, ["global", "department", "user"]),
        ("user", False, ["user"]),
    ],
    ids=["admin", "user"],
)
def test_skill_list_projects_real_permissions_and_allowed_levels(monkeypatch, role, can_manage, allowed_access_levels):
    """列表响应按真实权限逻辑投影管理标记和可设定共享范围。"""

    async def visible_skills(_repository, user):
        assert user.uid == role
        return [_skill_for_user(role)]

    monkeypatch.setattr(
        "yuxi.modules.extensions.skills.repository.SkillRepository.list_visible_for_management",
        visible_skills,
    )

    response = TestClient(_build_app(role=role)).get("/api/system/skills")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["success"] is True
    assert [(item["slug"], item["can_manage"]) for item in payload["data"]] == [("demo", can_manage)]
    assert payload["allowed_access_levels"] == allowed_access_levels


def test_builtin_routes_require_admin():
    """内置 Skill 路由通过生产管理员依赖拒绝普通用户。"""
    response = TestClient(_build_app(role="user")).get("/api/system/skills/builtin")

    assert response.status_code == 403
    assert response.json() == {"detail": "缺少功能权限: skill:manage"}


def test_builtin_sync_requires_manage_permission_without_calling_service(monkeypatch):
    """内置 Skill 同步在生产功能权限依赖处拒绝普通使用者。"""
    initializer = AsyncMock(side_effect=AssertionError("unauthorized request reached service"))
    monkeypatch.setattr("yuxi.api.routers.extensions.skills.init_builtin_skills", initializer)

    response = TestClient(_build_app(role="user")).post("/api/system/skills/builtin/sync")

    assert response.status_code == 403
    initializer.assert_not_awaited()


def test_builtin_sync_passes_superadmin_to_service(monkeypatch):
    """同步路由把实际认证用户传给 Skill 初始化服务并装配响应。"""
    initializer = AsyncMock(return_value=[])
    monkeypatch.setattr("yuxi.api.routers.extensions.skills.init_builtin_skills", initializer)

    response = TestClient(_build_app(role="superadmin")).post("/api/system/skills/builtin/sync")

    assert response.status_code == 200
    assert response.json() == {"success": True, "data": []}
    initializer.assert_awaited_once_with(None, created_by="superadmin")


def test_skill_content_conflict_maps_to_http_409(monkeypatch):
    """Skill 修订冲突通过路由稳定映射为 HTTP 409。"""
    saver = AsyncMock(side_effect=SkillEditConflict("revision changed"))
    monkeypatch.setattr("yuxi.api.routers.extensions.skills.save_skill_content", saver)

    response = TestClient(_build_app()).put(
        "/api/system/skills/demo/content",
        json={"expected_revision": "revision-1", "changes": []},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "revision changed"}
    saver.assert_awaited_once()


def test_skill_upload_validation_returns_displayable_business_error(tmp_path, monkeypatch):
    """真实解析拒绝非法 slug，并返回前端可展示的业务 detail。"""
    monkeypatch.setenv("YUXI_RUNTIME_DIR", str(tmp_path))
    response = TestClient(_build_app()).post(
        "/api/skills/import/prepare",
        files={"file": ("SKILL.md", b"---\nname: demo\nslug: Invalid_Slug\ndescription: demo\n---\n", "text/markdown")},
    )
    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "skill_request_invalid",
            "message": "SKILL.md frontmatter.slug 必须是小写字母/数字/短横线，且不能连续短横线",
        }
    }


@pytest.mark.parametrize(
    "metadata", ['name: demo\ndescription: "PRIVATE_YAML_MARKER', 'name: "PRIVATE_YAML_MARKER\ndescription:\n  note: value']
)
def test_skill_upload_yaml_error_does_not_echo_input(tmp_path, monkeypatch, metadata):
    """YAML 两条失败路径都只返回安全语法提示。"""
    monkeypatch.setenv("YUXI_RUNTIME_DIR", str(tmp_path))
    response = TestClient(_build_app()).post(
        "/api/skills/import/prepare",
        files={"file": ("SKILL.md", f"---\n{metadata}\n---\n".encode(), "text/markdown")},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == {
        "code": "skill_request_invalid",
        "message": "SKILL.md frontmatter YAML 解析失败，请检查 YAML 语法",
    }
    assert "PRIVATE_YAML_MARKER" not in response.text


def test_disabled_owned_and_enabled_shared_cards_remain_readonly_without_manage_permission(monkeypatch):
    """保留fork卡片展示，功能权限撤销后不得把资源manage_scope当管理授权。"""
    owned = _skill_for_user("user")
    owned.slug = "owned-disabled"
    owned.enabled = False
    shared = _skill_for_user("other")
    shared.slug = "shared"
    shared.share_config = {"version": 2, "read_scope": {"access_level": "user", "user_uids": ["user"]}, "manage_scope": None}

    async def visible(repository, user):
        assert user.uid == "user"
        return [owned, shared]

    monkeypatch.setattr("yuxi.modules.extensions.skills.repository.SkillRepository.list_visible_for_management", visible)
    response = TestClient(_build_app(role="user")).get("/api/system/skills")
    assert response.status_code == 200, response.text
    assert [(item["slug"], item["can_manage"]) for item in response.json()["data"]] == [("owned-disabled", False), ("shared", False)]
    assert response.json()["allowed_access_levels"] == ["user"]


def test_accessible_skills_route_preserves_current_authenticated_user(monkeypatch):
    """使用接口把当前身份交给真正可执行Skill目录，不能替换为管理员身份。"""

    async def accessible(db, user):
        assert user.uid == "user"
        return [_skill_for_user("user")]

    monkeypatch.setattr("yuxi.api.routers.extensions.skills.list_accessible_skills", accessible)
    response = TestClient(_build_app(role="user")).get("/api/skills/accessible")
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["slug"] == "demo"
