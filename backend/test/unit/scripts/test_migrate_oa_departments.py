from scripts.migrate_oa_departments import OaDepartment, _flatten, build_department_actions


def test_flatten_preserves_parent_ids():
    assert _flatten([{"id": 1, "treeCode": "001", "treeName": "研发", "children": [{"id": 2, "treeCode": "001001", "treeName": "平台"}]}]) == [
        OaDepartment(1, "001", "研发", None),
        OaDepartment(2, "001001", "平台", 1),
    ]


def test_existing_and_duplicate_departments_are_skipped():
    actions = build_department_actions(
        [OaDepartment(1, "001", "研发", None), OaDepartment(2, "001", "研发", None)],
        [(5, "研发", 1, None, None)],
    )
    assert [item.action for item in actions] == ["update_identity", "skip"]


def test_same_name_under_different_parents_is_mapped_by_parent_path():
    actions = build_department_actions(
        [OaDepartment(1, "001", "甲", None), OaDepartment(2, "001001", "质量部", 1)],
        [(5, "甲", 1, "001", 1), (6, "质量部", 5, None, None), (7, "质量部", 1, None, None)],
    )
    assert [item.action for item in actions] == ["skip", "update_identity"]
