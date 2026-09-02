from datetime import datetime

import pytest

from scripts.migrate_jd_ai_h5_conversations import (
    H5SourceReader,
    MigrationStats,
    build_message_payload,
    build_yuxi_uid,
    prepare_records,
)


class _FakeCursor:
    def __init__(self) -> None:
        self.sql = ""
        self.params = ()

    def execute(self, sql, params) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()

    def cursor(self):
        return self.cursor_instance


def test_build_yuxi_uid_uses_oa_account_format() -> None:
    assert build_yuxi_uid("2024102811") == "oa:ZD:2024102811"


def test_build_yuxi_uid_rejects_empty_account() -> None:
    with pytest.raises(ValueError, match="user_name"):
        build_yuxi_uid(" ")


def test_build_message_payload_maps_human_query_and_json_metadata() -> None:
    payload = build_message_payload(
        {
            "id": 7,
            "conversation_id": "c-1",
            "from_source": "human",
            "query_content": "你好",
            "create_time": datetime(2026, 8, 25, 10, 0, 0),
            "references": '[{"title":"文档"}]',
        },
        files=[{"id": 9, "file_name": "a.png", "file_size": 12}],
    )

    assert payload["role"] == "user"
    assert payload["content"] == "你好"
    assert payload["delivery_status"] == "complete"
    assert payload["extra_metadata"]["source"]["references"] == [{"title": "文档"}]
    assert payload["extra_metadata"]["files"][0]["file_name"] == "a.png"


def test_build_message_payload_restores_h5_escaped_newlines_for_markdown() -> None:
    payload = build_message_payload(
        {
            "id": 8,
            "conversation_id": "c-1",
            "from_source": "assistant",
            "answer": "第一段\\n\\n- 第二段\\r\\n第三段",
            "create_time": datetime(2026, 8, 25, 10, 0, 0),
        },
        files=[],
    )

    assert payload["role"] == "assistant"
    assert payload["content"] == "第一段\n\n- 第二段\n第三段"


def test_prepare_records_records_unknown_message_source_without_partial_conversation() -> None:
    stats = MigrationStats()
    records = prepare_records(
        conversations=[
            {
                "conversation_id": "c-1",
                "user_id": 1,
                "app_id": 1859570229117022213,
                "conversation_name": "测试会话",
                "create_time": datetime(2026, 8, 25, 10, 0, 0),
                "is_top": 0,
            }
        ],
        messages=[
            {
                "id": 1,
                "conversation_id": "c-1",
                "from_source": "unknown",
                "create_time": datetime(2026, 8, 25, 10, 0, 1),
            }
        ],
        files=[],
        users={"1": {"user_id": 1, "user_name": "2024102811"}},
        company_code="ZD",
        batch_id="test-batch",
        cutoff=datetime(2026, 8, 25, 12, 0, 0),
        stats=stats,
    )

    assert records == []
    assert stats.prepared_conversations == 0
    assert stats.failures[0]["stage"] == "source_validation"


def test_source_queries_are_scoped_to_selected_conversations() -> None:
    connection = _FakeConnection()
    reader = H5SourceReader(connection)
    cutoff = datetime(2026, 8, 25, 12, 0, 0)

    reader.fetch_messages(["c-1", "c-2"], cutoff)
    assert "conversation_id IN (%s, %s)" in connection.cursor_instance.sql
    assert connection.cursor_instance.params[-2:] == ("c-1", "c-2")

    reader.fetch_files(["c-1"], cutoff)
    assert "m.conversation_id IN (%s)" in connection.cursor_instance.sql
    assert connection.cursor_instance.params[-1] == "c-1"
