"""角色功能权限与数据范围的服务端目录。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionDefinition:
    """一项可分配给角色的功能权限。"""

    key: str
    name: str
    group: str
    description: str


@dataclass(frozen=True)
class DataScopeDefinition:
    """一种固定的数据范围。"""

    key: str
    label: str
    description: str


@dataclass(frozen=True)
class BuiltinRoleDefinition:
    """一个受保护的内置角色及其初始授权。"""

    code: str
    name: str
    description: str
    default_scope_type: str
    permission_keys: tuple[str, ...]


PERMISSION_CATALOG = (
    PermissionDefinition("role:read", "查看角色", "角色与用户", "查看角色、功能权限、默认数据范围和成员"),
    PermissionDefinition("role:manage", "管理角色", "角色与用户", "创建、复制、修改和停用自定义角色"),
    PermissionDefinition("user:read", "查看用户", "角色与用户", "查看授权数据范围内的用户"),
    PermissionDefinition("user:create", "创建用户", "角色与用户", "在授权数据范围内创建用户"),
    PermissionDefinition("user:update", "修改用户", "角色与用户", "修改授权数据范围内的用户"),
    PermissionDefinition("user:delete", "删除用户", "角色与用户", "删除授权数据范围内的用户"),
    PermissionDefinition("user:role_assign", "分配角色", "角色与用户", "为授权数据范围内的用户分配角色"),
    PermissionDefinition("user:impersonate", "模拟用户", "角色与用户", "以目标用户身份执行调试操作"),
    PermissionDefinition("department:read", "查看组织机构", "组织机构", "查看授权数据范围内的组织树"),
    PermissionDefinition("department:read_all", "查看全部组织机构", "组织机构", "查看完整集团组织机构树"),
    PermissionDefinition("department:create", "创建组织节点", "组织机构", "在组织机构树中创建节点"),
    PermissionDefinition("department:update", "修改组织节点", "组织机构", "修改或移动组织节点"),
    PermissionDefinition("department:delete", "删除组织节点", "组织机构", "删除满足保护条件的组织节点"),
    PermissionDefinition("dashboard:view", "查看 Dashboard", "统计分析", "查看授权数据范围内的统计信息"),
    PermissionDefinition("system_config:manage", "管理系统配置", "平台管理", "修改系统级配置"),
    PermissionDefinition("system_log:read", "查看系统日志", "平台管理", "查看系统运行日志"),
    PermissionDefinition("system_task:manage", "管理系统任务", "平台管理", "查看、取消和删除系统任务"),
    PermissionDefinition("model_provider:manage", "管理模型供应商", "平台管理", "维护模型供应商及模型配置"),
    PermissionDefinition("tool:manage", "管理工具", "平台管理", "查看和配置系统工具"),
    PermissionDefinition("mcp:manage", "管理 MCP", "平台管理", "维护 MCP 服务"),
    PermissionDefinition("graph:manage", "管理知识图谱", "平台管理", "查看和管理知识图谱"),
    PermissionDefinition("ocr:manage", "管理 OCR", "平台管理", "维护 OCR 配置"),
    PermissionDefinition("api_key:manage_all", "管理全部 API Key", "平台管理", "管理其他用户的 API Key"),
    PermissionDefinition("knowledge_base:read", "使用知识库", "业务资源", "读取共享范围允许访问的知识库"),
    PermissionDefinition("knowledge_base:manage", "管理知识库", "业务资源", "管理共享范围允许维护的知识库"),
    PermissionDefinition("knowledge_evaluation:manage", "管理知识库评估", "业务资源", "执行和管理知识库评估"),
    PermissionDefinition("agent:use", "使用智能体", "业务资源", "使用共享范围允许访问的智能体"),
    PermissionDefinition("agent:manage", "管理智能体", "业务资源", "管理共享范围允许维护的智能体"),
    PermissionDefinition("skill:use", "使用 Skill", "业务资源", "使用共享范围允许访问的 Skill"),
    PermissionDefinition("skill:manage", "管理 Skill", "业务资源", "管理共享范围允许维护的 Skill"),
)

DATA_SCOPE_CATALOG = (
    DataScopeDefinition("none", "无数据", "不允许访问任何组织数据"),
    DataScopeDefinition("self", "仅本人", "仅访问明确归属于本人的数据"),
    DataScopeDefinition(
        "organization_and_descendants",
        "本组织及下级",
        "访问当前归属节点自身及其全部后代组织的数据",
    ),
    DataScopeDefinition(
        "selected_organizations_and_descendants",
        "指定组织及下级",
        "访问指定组织节点自身及其全部后代组织的数据",
    ),
    DataScopeDefinition("all", "全部数据", "访问整棵组织机构树的数据"),
)

ALL_PERMISSION_KEYS = tuple(item.key for item in PERMISSION_CATALOG)

USER_PERMISSION_KEYS = (
    "knowledge_base:read",
    "agent:use",
    "skill:use",
)

ADMIN_PERMISSION_KEYS = (
    "role:read",
    "user:read",
    "user:create",
    "user:update",
    "user:delete",
    "user:role_assign",
    "department:read",
    "system_config:manage",
    "system_log:read",
    "system_task:manage",
    "model_provider:manage",
    "tool:manage",
    "mcp:manage",
    "graph:manage",
    "ocr:manage",
    "knowledge_base:read",
    "knowledge_base:manage",
    "knowledge_evaluation:manage",
    "agent:use",
    "agent:manage",
    "skill:use",
    "skill:manage",
)

BUILTIN_ROLES = (
    BuiltinRoleDefinition(
        code="superadmin",
        name="超级管理员",
        description="拥有全部功能权限和全部数据范围",
        default_scope_type="all",
        permission_keys=ALL_PERMISSION_KEYS,
    ),
    BuiltinRoleDefinition(
        code="admin",
        name="管理员",
        description="保留现有管理能力，默认覆盖本组织及下级",
        default_scope_type="organization_and_descendants",
        permission_keys=ADMIN_PERMISSION_KEYS,
    ),
    BuiltinRoleDefinition(
        code="user",
        name="普通用户",
        description="保留普通使用能力，组织数据默认仅本人",
        default_scope_type="self",
        permission_keys=USER_PERMISSION_KEYS,
    ),
)
