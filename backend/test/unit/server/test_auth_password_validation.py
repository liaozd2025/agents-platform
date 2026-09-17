import pytest
from pydantic import ValidationError

from server.routers.auth_dept_router import DepartmentCreate
from server.routers.auth_router import InitializeAdmin, UserCreate, UserPasswordChange, UserUpdate


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (InitializeAdmin, {"uid": "admin", "password": "short"}),
        (UserCreate, {"username": "user", "password": "short"}),
        (UserUpdate, {"password": "short"}),
        # 自助改密：新密码受 8 位下限约束
        (UserPasswordChange, {"old_password": "old-password", "new_password": "short"}),
        (
            DepartmentCreate,
            {
                "name": "department",
                "admin_uid": "admin",
                "admin_password": "short",
            },
        ),
    ],
)
def test_admin_password_models_reject_passwords_shorter_than_eight_characters(model, payload):
    with pytest.raises(ValidationError) as exc_info:
        model.model_validate(payload)

    assert exc_info.value.errors()[0]["type"] == "string_too_short"


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (InitializeAdmin, {"uid": "admin", "password": "12345678"}),
        (UserCreate, {"username": "user", "password": "12345678"}),
        (UserUpdate, {"password": "12345678"}),
        (UserPasswordChange, {"old_password": "old-password", "new_password": "12345678"}),
        (
            DepartmentCreate,
            {
                "name": "department",
                "admin_uid": "admin",
                "admin_password": "12345678",
            },
        ),
    ],
)
def test_admin_password_models_accept_eight_character_passwords(model, payload):
    assert model.model_validate(payload)


def test_user_update_allows_password_to_be_omitted():
    assert UserUpdate().password is None


def test_password_change_allows_short_old_password():
    """原密码只要求非空：历史密码可能由管理员设置、不受当前 8 位规则约束。"""

    payload = UserPasswordChange.model_validate({"old_password": "x", "new_password": "12345678"})
    assert payload.old_password == "x"


def test_password_change_rejects_empty_old_password_and_extra_fields():
    """原密码不能为空，且额外字段要被拒绝（extra=forbid，避免前端误传字段被静默忽略）。"""

    with pytest.raises(ValidationError):
        UserPasswordChange.model_validate({"old_password": "", "new_password": "12345678"})

    with pytest.raises(ValidationError):
        UserPasswordChange.model_validate(
            {"old_password": "old-password", "new_password": "12345678", "user_id": 2}
        )
