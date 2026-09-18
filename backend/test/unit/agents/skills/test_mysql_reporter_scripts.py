from __future__ import annotations

import importlib.util
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

from yuxi.agents.skills.buildin import BUILTIN_SKILLS


def _mysql_reporter_dir() -> Path:
    for spec in BUILTIN_SKILLS:
        if spec.slug == "mysql-reporter":
            return spec.source_dir
    raise AssertionError("mysql-reporter builtin skill spec not found")


def _load_script(script_name: str) -> ModuleType:
    script_path = _mysql_reporter_dir() / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(f"mysql_reporter_{script_path.stem}", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mysql_reporter_query_security_validates_sql_and_timeout():
    query_script = _load_script("query.py")
    sql_cases = {
        "": False,
        "SELECT * FROM users": True,
        "show tables": True,
        "DESCRIBE users": True,
        "EXPLAIN SELECT * FROM users": True,
        "SELECT 1;": True,
        "DELETE FROM users": False,
        "SELECT * FROM users WHERE id = 1 OR 1=1": False,
        "SELECT * FROM users UNION SELECT password FROM admin": False,
        "SELECT 'DROP' AS keyword_text": True,
        "/* comment */ SELECT 1": True,
        "/* multi\nline */ SELECT 1": True,
        "SELECT * FROM users; DROP TABLE users": False,
        "SELECT * FROM users; CREATE TABLE audit_log(id INT)": False,
        "SELECT * FROM users; SET @unsafe = 1": False,
        "SELECT 'report' INTO OUTFILE '/tmp/report.txt'": False,
        "SELECT 'report' INTO/**/DUMPFILE '/tmp/report.bin'": False,
        "SELECT LOAD_FILE('/etc/passwd')": False,
        "SELECT LOAD_FILE/**/('/etc/passwd')": False,
        "SELECT '-- comment' INTO OUTFILE '/tmp/report.txt'": False,
        "SELECT 1 INTO # comment\n OUTFILE '/tmp/report.txt'": False,
        "SELECT 'report' /*!50000 INTO OUTFILE '/tmp/report.txt' */": False,
        "SELECT 1 /*M!100100 INTO OUTFILE '/tmp/report.txt' */": False,
        "SELECT 1 /*!50000 + 1 */": False,
        "SELECT 1 /*M!100100 + 1 */": False,
    }

    for sql, expected in sql_cases.items():
        assert query_script.MySQLSecurityChecker.validate_sql(sql) is expected

    timeout_cases = {
        None: False,
        0: False,
        1: True,
        60: True,
        600: True,
        601: False,
        "60": False,
    }

    for timeout, expected in timeout_cases.items():
        assert query_script.MySQLSecurityChecker.validate_timeout(timeout) is expected


def test_mysql_reporter_describe_table_name_security_validates_known_cases():
    describe_script = _load_script("describe_table.py")
    table_cases = {
        "": False,
        "users": True,
        "_audit_log": True,
        "user_2026": True,
        "1users": False,
        "user-name": False,
        "users;drop": False,
    }

    for table_name, expected in table_cases.items():
        assert describe_script.MySQLSecurityChecker.validate_table_name(table_name) is expected


@pytest.mark.parametrize(
    ("rows", "displayed", "truncated"),
    [
        ([], 0, False),
        ([{"id": 1}], 1, False),
        ([{"id": i} for i in range(51)], 50, True),
        ([{"detail": "x" * 11000}], 0, True),
    ],
)
def test_query_preview_distinguishes_empty_and_truncated_results(rows, displayed, truncated):
    """预览截断不能把真实记录变成无数据。"""
    query_script = _load_script("query.py")
    output = query_script.format_query_result(rows)
    if rows:
        assert "数据为空" not in output
        assert "没有返回任何结果" not in output
    else:
        assert "没有返回任何结果" in output
    summary = json.loads(output.splitlines()[0])
    assert summary == {"row_count": len(rows), "displayed_row_count": displayed, "truncated": truncated}
    if truncated:
        assert "--output-json" in output
        assert "不能用于" in output


def test_query_cli_exports_complete_rows_without_overwriting(monkeypatch, tmp_path, capsys):
    """完整导出保留末行与长字段，且不覆盖已有文件或符号链接。"""
    query_script = _load_script("query.py")
    rows = [{"id": i, "amount": Decimal("1.23"), "day": date(2026, 9, 18)} for i in range(75)]
    rows[-1]["detail"] = "x" * 11000
    output_path = tmp_path / "rows.json"

    class Connection:
        open = True

        def close(self):
            """记录查询连接已经关闭。"""
            self.open = False

    connection = Connection()
    monkeypatch.setattr(query_script, "load_mysql_config", lambda: {})
    monkeypatch.setattr(query_script, "create_connection", lambda _config: connection)
    monkeypatch.setattr(query_script, "execute_query_with_timeout", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(
        query_script.sys, "argv", ["query.py", "--sql", "SELECT * FROM sales", "--output-json", str(output_path)]
    )
    assert query_script.main() == 0
    assert not connection.open
    exported = json.loads(output_path.read_text())
    assert exported["row_count"] == 75
    assert exported["truncated"] is False
    assert len(exported["rows"]) == 75
    assert exported["rows"][-1] == {"id": 74, "amount": "1.23", "day": "2026-09-18", "detail": "x" * 11000}
    assert json.loads(capsys.readouterr().out.splitlines()[0])["truncated"] is True

    original = output_path.read_bytes()
    assert query_script.main() == 1
    assert output_path.read_bytes() == original
    output_path.unlink()
    target = tmp_path / "existing.json"
    target.write_text("original")
    output_path.symlink_to(target)
    assert query_script.main() == 1
    assert target.read_text() == "original"
