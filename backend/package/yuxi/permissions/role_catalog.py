"""角色功能权限与数据范围的服务端目录。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionDefinition:
    """一项可分配给角色的功能权限。"""

    key: str
    name: str
    group: str
    description: str
    parent_key: str | None = None
    display_order: int = 0


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
    PermissionDefinition(
        "role:read",
        "角色与权限",
        "组织与权限",
        "查看角色、功能权限、默认数据范围和成员",
        display_order=40,
    ),
    PermissionDefinition("role:manage", "管理角色", "组织与权限", "创建、复制、修改和停用自定义角色", "role:read"),
    PermissionDefinition("user:read", "用户管理", "组织与权限", "查看授权数据范围内的用户", display_order=20),
    PermissionDefinition("user:create", "创建用户", "组织与权限", "在授权数据范围内创建用户", "user:read"),
    PermissionDefinition("user:update", "修改用户", "组织与权限", "修改授权数据范围内的用户", "user:read"),
    PermissionDefinition("user:delete", "删除用户", "组织与权限", "删除授权数据范围内的用户", "user:read"),
    PermissionDefinition(
        "user:role_assign",
        "分配角色",
        "组织与权限",
        "为授权数据范围内的用户分配角色",
        "user:read",
    ),
    PermissionDefinition("user:impersonate", "模拟用户", "组织与权限", "以目标用户身份执行调试操作", "user:read"),
    PermissionDefinition(
        "department:read",
        "组织机构",
        "组织与权限",
        "查看授权数据范围内的组织树",
        display_order=30,
    ),
    PermissionDefinition(
        "department:read_all",
        "查看全部组织",
        "组织与权限",
        "查看完整集团组织机构树",
        "department:read",
    ),
    PermissionDefinition(
        "department:create",
        "创建节点",
        "组织与权限",
        "在组织机构树中创建节点",
        "department:read",
    ),
    PermissionDefinition("department:update", "修改节点", "组织与权限", "修改或移动组织节点", "department:read"),
    PermissionDefinition(
        "department:delete",
        "删除节点",
        "组织与权限",
        "删除满足保护条件的组织节点",
        "department:read",
    ),
    PermissionDefinition(
        "dashboard:view",
        "概览 Dashboard",
        "统计分析",
        "查看授权数据范围内的统计信息",
        display_order=10,
    ),
    PermissionDefinition("system_config:manage", "系统配置", "平台管理", "修改系统级配置", display_order=140),
    PermissionDefinition("system_log:read", "系统日志", "平台管理", "查看系统运行日志", display_order=150),
    PermissionDefinition("system_task:manage", "系统任务", "平台管理", "查看、取消和删除系统任务", display_order=160),
    PermissionDefinition(
        "model_provider:manage",
        "模型供应商",
        "平台管理",
        "维护模型供应商及模型配置",
        display_order=90,
    ),
    PermissionDefinition("tool:manage", "工具", "平台管理", "查看和配置系统工具", display_order=100),
    PermissionDefinition("mcp:manage", "MCP 服务", "平台管理", "维护 MCP 服务", display_order=110),
    PermissionDefinition("graph:manage", "知识图谱", "业务资源", "查看和管理知识图谱", display_order=80),
    PermissionDefinition("ocr:manage", "OCR 配置", "平台管理", "维护 OCR 配置", display_order=120),
    PermissionDefinition("api_key:manage_all", "API Keys", "平台管理", "管理其他用户的 API Key", display_order=130),
    PermissionDefinition(
        "knowledge_base:read",
        "知识库",
        "业务资源",
        "读取共享范围允许访问的知识库",
        display_order=50,
    ),
    PermissionDefinition(
        "knowledge_base:manage",
        "管理知识库",
        "业务资源",
        "管理共享范围允许维护的知识库",
        "knowledge_base:read",
    ),
    PermissionDefinition(
        "knowledge_evaluation:manage",
        "评估知识库",
        "业务资源",
        "执行和管理知识库评估",
        "knowledge_base:read",
    ),
    PermissionDefinition("agent:use", "智能体", "业务资源", "使用共享范围允许访问的智能体", display_order=60),
    PermissionDefinition("agent:manage", "管理智能体", "业务资源", "管理共享范围允许维护的智能体", "agent:use"),
    PermissionDefinition("skill:use", "Skill", "业务资源", "使用共享范围允许访问的 Skill", display_order=70),
    PermissionDefinition("skill:manage", "管理 Skill", "业务资源", "管理共享范围允许维护的 Skill", "skill:use"),
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
