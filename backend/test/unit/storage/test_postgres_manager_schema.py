"""当前业务与知识 Schema 的只读契约。"""

from unittest.mock import AsyncMock

import pytest

from yuxi.infrastructure.postgres.models import load_models
from yuxi.infrastructure.postgres.base import BusinessBase, KnowledgeBase
from yuxi.infrastructure.postgres import schema as schema_versions
from yuxi.modules.agents.models.runs import AgentRun


def test_business_and_knowledge_metadata_are_disjoint():
    """Schema 初始化分别创建业务与知识域 metadata。"""
    load_models()

    assert BusinessBase is not KnowledgeBase
    assert {"users", "projects", "agent_runs"} <= set(BusinessBase.metadata.tables)
    assert {"knowledge_bases", "evaluation_runs"} <= set(KnowledgeBase.metadata.tables)
    assert "knowledge_bases" not in BusinessBase.metadata.tables
    assert "users" not in KnowledgeBase.metadata.tables


def test_current_project_and_run_shape_is_declared_in_metadata():
    """新库建表所需的项目与 Run 字段由业务 ORM 拥有。"""
    load_models()
    projects = BusinessBase.metadata.tables["projects"]
    runs = BusinessBase.metadata.tables["agent_runs"]

    assert projects.c.status.nullable is False
    assert "deleted_at" in projects.c
    assert "ck_projects_status" in {constraint.name for constraint in projects.constraints}
    assert "fk_projects_uid_users" in {constraint.name for constraint in projects.foreign_key_constraints}
    assert {"turn_id", "runtime_scope_id", "execution_seq"} <= set(runs.c.keys())
    assert "last_event_id" not in AgentRun.__table__.c


@pytest.mark.asyncio
async def test_runtime_rejects_missing_or_incompatible_schema_versions(monkeypatch):
    """运行进程只接受两个域均精确匹配当前版本。"""
    versions = AsyncMock(return_value={})
    monkeypatch.setattr(schema_versions, "get_schema_versions", versions)
    manager = object()

    with pytest.raises(RuntimeError, match=r"business=missing .*knowledge=missing"):
        await schema_versions.require_current_schema(manager)

    versions.return_value = {"business": 99, "knowledge": schema_versions.KNOWLEDGE_SCHEMA_VERSION}
    with pytest.raises(RuntimeError, match="business=99"):
        await schema_versions.require_current_schema(manager)

    versions.return_value = {
        "business": schema_versions.BUSINESS_SCHEMA_VERSION,
        "knowledge": schema_versions.KNOWLEDGE_SCHEMA_VERSION,
    }
    await schema_versions.require_current_schema(manager)


def test_ownership_columns_can_store_enterprise_identity_without_truncation():
    """所有 UID 与归属快照列须容纳真实 OIDC 身份及外部用户标识。"""
    load_models()
    required = max(len("oidc:" + "a" * 64), len("endusr_" + "a" * 64))
    for base in (BusinessBase, KnowledgeBase):
        for table in base.metadata.tables.values():
            for column in table.columns:
                if column.name in {"uid", "created_by", "updated_by"}:
                    length = getattr(column.type, "length", None)
                    assert length is None or length >= required, f"{table.name}.{column.name} truncates generated identity"


def test_fresh_enterprise_schema_retains_oa_identity_and_organization_snapshots():
    """当前新库保留企业身份与快照字段，不把旧 ALTER/回填语句当作初始化契约。"""
    load_models()
    users = BusinessBase.metadata.tables["users"]
    departments = BusinessBase.metadata.tables["departments"]
    assert "role" not in users.c
    for name in ("display_name", "oa_station_name", "oa_job_level_name"):
        assert users.c[name].type.length == 100
    assert departments.c.oa_department_code.type.length == 64
    for name in ("oa_department_code", "oa_department_id"):
        assert any(index.unique and name in index.columns for index in departments.indexes)
    for table_name in (
        "roles",
        "role_permissions",
        "role_default_departments",
        "user_role_assignments",
        "user_role_assignment_departments",
    ):
        assert table_name in BusinessBase.metadata.tables
    for metadata, names in (
        (
            BusinessBase.metadata,
            ("sessions", "agent_runs", "messages", "operation_logs", "security_audits", "agents", "skills"),
        ),
        (KnowledgeBase.metadata, ("knowledge_bases",)),
    ):
        for name in names:
            columns = metadata.tables[name].c
            assert {"organization_id_snapshot", "organization_path_snapshot", "organization_snapshot_inferred"} <= set(columns.keys())
            assert columns.organization_snapshot_inferred.nullable is False

    feedback = BusinessBase.metadata.tables["agent_feedback_records"]
    assert any(foreign_key.target_fullname == "agent_runs.id" for foreign_key in feedback.c.run_id.foreign_keys)
