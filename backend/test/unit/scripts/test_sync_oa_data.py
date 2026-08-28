from argparse import Namespace
from unittest.mock import AsyncMock, Mock

import pytest

from scripts.migrate_oa_departments import DepartmentAction, OaDepartment
from scripts.migrate_oa_users import MigrationAction, OaUser
from scripts.sync_oa_data import build_department_url, run


def test_build_department_url_requires_user_endpoint():
    assert build_department_url("https://example.test/DrugDevp/QueryUserPage") == (
        "https://example.test/DrugDevp/QueryDepartmentTree"
    )
    with pytest.raises(ValueError):
        build_department_url("https://example.test/DrugDevp/Other")


@pytest.mark.asyncio
async def test_dry_run_simulates_department_mapping_without_writing(monkeypatch):
    args = Namespace(
        url="https://example.test/DrugDevp/QueryUserPage",
        license_file="/run/secrets/oa-license.lic",
        timeout=10,
        company_code="ZD",
        apply=False,
    )
    department_action = DepartmentAction("create", 100, "001", "研发部")
    user_actions = [MigrationAction("create", "alice", department_id=-100)]

    monkeypatch.setattr(
        "scripts.sync_oa_data.user_migration.fetch_oa_access_code", Mock(return_value="runtime-code")
    )
    monkeypatch.setattr(
        "scripts.sync_oa_data.department_migration.fetch_oa_departments",
        Mock(return_value=[OaDepartment(100, "001", "研发部", None)]),
    )
    monkeypatch.setattr(
        "scripts.sync_oa_data.department_migration.load_database_departments", AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "scripts.sync_oa_data.department_migration.build_department_actions", Mock(return_value=[department_action]),
    )
    monkeypatch.setattr(
        "scripts.sync_oa_data.user_migration.load_database_inputs", AsyncMock(return_value=([], [])),
    )
    monkeypatch.setattr(
        "scripts.sync_oa_data.user_migration.fetch_oa_users", Mock(return_value=[OaUser("alice", "张三", "研发部", 100)]),
    )
    build_actions = Mock(return_value=user_actions)
    monkeypatch.setattr("scripts.sync_oa_data.user_migration.build_migration_actions", build_actions)
    department_apply = AsyncMock()
    user_apply = AsyncMock()
    monkeypatch.setattr("scripts.sync_oa_data.department_migration.apply_actions", department_apply)
    monkeypatch.setattr("scripts.sync_oa_data.user_migration.apply_actions", user_apply)

    assert await run(args) == 0
    assert build_actions.call_args.args[1] == [(-100, 100)]
    department_apply.assert_not_awaited()
    user_apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_writes_departments_then_reloads_real_mapping_before_users(monkeypatch):
    args = Namespace(
        url="https://example.test/DrugDevp/QueryUserPage",
        license_file="/run/secrets/oa-license.lic",
        timeout=10,
        company_code="ZD",
        apply=True,
    )
    events: list[str] = []
    monkeypatch.setattr(
        "scripts.sync_oa_data.user_migration.fetch_oa_access_code", Mock(return_value="runtime-code")
    )
    monkeypatch.setattr(
        "scripts.sync_oa_data.department_migration.fetch_oa_departments", Mock(return_value=[])
    )
    monkeypatch.setattr(
        "scripts.sync_oa_data.department_migration.load_database_departments", AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "scripts.sync_oa_data.department_migration.build_department_actions",
        Mock(return_value=[DepartmentAction("create", 100, "001", "研发部")]),
    )

    async def apply_departments(actions):
        events.append("departments")

    monkeypatch.setattr("scripts.sync_oa_data.department_migration.apply_actions", apply_departments)
    monkeypatch.setattr(
        "scripts.sync_oa_data.user_migration.load_database_inputs",
        AsyncMock(return_value=([(23, 100)], [])),
    )
    monkeypatch.setattr("scripts.sync_oa_data.user_migration.fetch_oa_users", Mock(return_value=[]))
    build_actions = Mock(return_value=[MigrationAction("create", "alice", department_id=23)])
    monkeypatch.setattr("scripts.sync_oa_data.user_migration.build_migration_actions", build_actions)

    async def apply_users(actions):
        events.append("users")

    monkeypatch.setattr("scripts.sync_oa_data.user_migration.apply_actions", apply_users)

    assert await run(args) == 0
    assert build_actions.call_args.args[1] == [(23, 100)]
    assert events == ["departments", "users"]


@pytest.mark.asyncio
async def test_department_conflict_stops_before_any_write(monkeypatch):
    args = Namespace(
        url="https://example.test/DrugDevp/QueryUserPage",
        license_file="/run/secrets/oa-license.lic",
        timeout=10,
        company_code="ZD",
        apply=True,
    )
    monkeypatch.setattr(
        "scripts.sync_oa_data.user_migration.fetch_oa_access_code", Mock(return_value="runtime-code")
    )
    monkeypatch.setattr("scripts.sync_oa_data.department_migration.fetch_oa_departments", Mock(return_value=[]))
    monkeypatch.setattr("scripts.sync_oa_data.department_migration.load_database_departments", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "scripts.sync_oa_data.department_migration.build_department_actions",
        Mock(return_value=[DepartmentAction("conflict", 100, "001", "研发部")]),
    )
    department_apply = AsyncMock()
    user_apply = AsyncMock()
    monkeypatch.setattr("scripts.sync_oa_data.department_migration.apply_actions", department_apply)
    monkeypatch.setattr("scripts.sync_oa_data.user_migration.apply_actions", user_apply)

    assert await run(args) == 1
    department_apply.assert_not_awaited()
    user_apply.assert_not_awaited()
