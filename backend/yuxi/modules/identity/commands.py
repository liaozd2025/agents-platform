"""用户资料与角色分配的写入命令。"""

from pydantic import BaseModel, ConfigDict, Field


class UserRoleAssignmentRequest(BaseModel):
    """一条用户角色分配及其可选收窄范围。"""

    role_id: int
    scope_mode: str = Field(default="inherit", pattern="^(inherit|override)$")
    override_scope_type: str | None = Field(default=None, max_length=64)
    override_department_ids: list[int] = Field(default_factory=list)


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str = Field(min_length=8)
    phone_number: str | None = None
    department_id: int | None = None
    role_assignments: list[UserRoleAssignmentRequest] | None = None
    reason: str | None = Field(default=None, max_length=500)


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str | None = None
    password: str | None = Field(default=None, min_length=8)
    phone_number: str | None = None
    avatar: str | None = None
    department_id: int | None = None
    role_assignments: list[UserRoleAssignmentRequest] | None = None
    reason: str | None = Field(default=None, max_length=500)


class UserProfileUpdate(BaseModel):
    username: str | None = None
    phone_number: str | None = None


class UserPasswordChange(BaseModel):
    """普通用户自助修改密码的请求体。

    原密码仅校验非空：历史密码可能由管理员设置且长度不受当前规则约束，
    这里不做长度限制，只要求必须填写以便服务端做真实校验。
    """

    model_config = ConfigDict(extra="forbid")
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


class UsernameValidation(BaseModel):
    """校验登录账号并生成UID的请求。"""

    username: str
