import sys
from unittest.mock import MagicMock, patch

from scripts.migrate_oa_users import (
    OaUser,
    build_migration_actions,
    fetch_oa_access_code,
    fetch_oa_users,
    normalize_oa_department_id,
    parse_args,
)


def test_unique_department_creates_user():
    actions = build_migration_actions([OaUser("alice", "Alice", "研发部", 200004)], [(3, 200004)], [])
    assert actions[0].action == "create"
    assert actions[0].department_id == 3
    assert actions[0].uid == "oa:ZD:alice"
    assert actions[0].display_name == "Alice"


def test_legacy_uid_is_repaired_to_stable_oa_uid():
    actions = build_migration_actions(
        [OaUser("alice", "Alice", "研发部", 200004)], [(3, 200004)], [(8, "alice", 3, "oa-legacy")]
    )
    assert actions[0].action == "update_identity"
    assert actions[0].uid == "oa:ZD:alice"
    assert actions[0].display_name == "Alice"


def test_stable_uid_conflict_is_reported():
    actions = build_migration_actions(
        [OaUser("alice", "Alice", "研发部", 200004)],
        [(3, 200004)],
        [(8, "alice", 3, "oa:ZD:alice"), (9, "other", 3, "oa:ZD:alice")],
    )
    assert actions[0].action == "conflict"


def test_missing_or_duplicate_department_id_skips_user():
    users = [OaUser("alice", "Alice", "不存在", 404), OaUser("bob", "Bob", "研发部", 200004)]
    actions = build_migration_actions(users, [(3, 200004), (4, 200004)], [])
    assert [action.reason for action in actions] == ["部门 ID 不存在", "部门 ID 重复"]


def test_existing_user_updates_department_and_missing_display_name():
    actions = build_migration_actions(
        [OaUser("alice", "Alice", "研发部", 200004)],
        [(3, 200004)],
        [(8, "alice", 2, "oa:ZD:alice")],
    )
    assert actions == [actions[0]]
    assert actions[0].action == "update_identity"
    assert actions[0].department_id == 3
    assert actions[0].display_name == "Alice"
    assert not hasattr(actions[0], "password_hash")


def test_existing_user_updates_changed_display_name_without_changing_identity():
    actions = build_migration_actions(
        [OaUser("alice", "Alice New", "研发部", 200004)],
        [(3, 200004)],
        [(8, "alice", 3, "oa:ZD:alice", "Alice Old")],
    )

    assert actions[0].action == "update_identity"
    assert actions[0].department_id is None
    assert actions[0].uid is None
    assert actions[0].display_name == "Alice New"


def test_empty_and_duplicate_accounts_are_skipped():
    users = [
        OaUser("", "Alice", "研发部", 200004),
        OaUser("alice", "", "研发部", 200004),
        OaUser("alice", "Alice", "研发部", 200004),
        OaUser("alice", "Alice", "研发部", 200004),
    ]
    actions = build_migration_actions(users, [(3, 200004)], [])
    assert [action.reason for action in actions] == ["账号为空", "姓名为空", None, "OA 账号重复"]


def test_normalize_oa_department_id_accepts_float_style_id_and_rejects_invalid_values():
    assert normalize_oa_department_id("200004.0") == 200004
    assert normalize_oa_department_id("200004.5") is None
    assert normalize_oa_department_id("invalid") is None


def test_fetch_oa_users_prefers_nickname_as_display_name():
    """旧 OA 的 nickName 是 sys_user.nick_name 对应的真实姓名，优先于其他旧字段。"""
    response = MagicMock()
    response.__enter__.return_value = response
    with patch("scripts.migrate_oa_users.urlopen", return_value=response), patch(
        "scripts.migrate_oa_users.json.load",
        return_value={
            "status": 1,
            "data": {
                "pageDatas": [
                    {
                        "account": "oa-account",
                        "nickName": "真实姓名",
                        "fullName": "旧姓名字段",
                        "departmentId": "200004.0",
                    }
                ]
            },
        },
    ):
        users = fetch_oa_users("https://example.test/oa-api/DrugDevp/QueryUserPage", "runtime-code", 10)

    assert users == [OaUser("oa-account", "真实姓名", "", 200004)]


def test_fetch_oa_access_code_reads_license_without_exposing_it(tmp_path):
    license_file = tmp_path / "license.lic"
    license_file.write_text("secret-value", encoding="utf-8")
    response = MagicMock()
    response.__enter__.return_value = response
    with patch("scripts.migrate_oa_users.urlopen", return_value=response) as urlopen_mock, patch(
        "scripts.migrate_oa_users.json.load", return_value={"status": 1, "data": "runtime-code"}
    ):
        code = fetch_oa_access_code(
            "https://example.test/oa-api/DrugDevp/QueryUserPage", str(license_file), 10
        )

    assert code == "runtime-code"
    request = urlopen_mock.call_args.args[0]
    assert request.full_url == "https://example.test/oa-api/SimpleAuth/GetCode"
    assert request.data == b'{"SecretKey": "secret-value"}'


def test_parse_args_does_not_accept_or_read_runtime_code(monkeypatch):
    """运行时 Code 不得通过命令行或环境变量进入迁移进程。"""

    monkeypatch.setenv("OA_USER_MIGRATION_CODE", "must-not-be-read")
    monkeypatch.setattr(sys, "argv", ["migrate_oa_users.py"])
    args = parse_args()

    assert not hasattr(args, "code")
