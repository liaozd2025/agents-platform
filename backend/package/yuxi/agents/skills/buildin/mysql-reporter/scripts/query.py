# /// script
# dependencies = [
#   "pymysql>=1.1.0",
# ]
# ///

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import pymysql
from pymysql import MySQLError
from pymysql.cursors import DictCursor


class MySQLConnectionError(Exception):
    """MySQL 连接异常"""


class QueryTimeoutError(Exception):
    """查询超时异常"""


class MySQLSecurityChecker:
    """MySQL 安全检查器"""

    ALLOWED_OPERATIONS = {"SELECT", "SHOW", "DESCRIBE", "EXPLAIN"}

    DANGEROUS_KEYWORDS = {
        "DROP",
        "DELETE",
        "UPDATE",
        "INSERT",
        "CREATE",
        "ALTER",
        "TRUNCATE",
        "REPLACE",
        "LOAD",
        "GRANT",
        "REVOKE",
        "SET",
        "COMMIT",
        "ROLLBACK",
        "UNLOCK",
        "KILL",
        "SHUTDOWN",
    }

    @classmethod
    def validate_sql(cls, sql: str) -> bool:
        """拒绝已知危险语法；数据库只读账号仍是最终权限边界。"""
        # MySQL/MariaDB 可执行注释不能当普通注释删掉后放行。
        if not sql or re.search(r"/\*(?:!|M!)", sql, re.IGNORECASE):
            return False
        # 在去注释前保守拒绝文件操作名，防止字符串中的注释符隐藏后续 SQL。
        if re.search(r"\b(?:OUTFILE|DUMPFILE|LOAD_FILE)\b", sql, re.IGNORECASE):
            return False

        sql_clean = re.sub(r"--.*$", "", sql, flags=re.MULTILINE)
        sql_clean = re.sub(r"/\*.*?\*/", " ", sql_clean, flags=re.DOTALL)
        sql_upper = sql_clean.strip().upper()
        sql_without_trailing_semicolon = sql_upper.rstrip()
        if sql_without_trailing_semicolon.endswith(";"):
            sql_without_trailing_semicolon = sql_without_trailing_semicolon[:-1].rstrip()
        if ";" in sql_without_trailing_semicolon:
            return False

        if not any(sql_upper.startswith(op) for op in cls.ALLOWED_OPERATIONS):
            return False

        first_word_match = re.match(r"^\s*(\w+)", sql_upper)
        first_word = first_word_match.group(1) if first_word_match else ""
        if first_word in cls.DANGEROUS_KEYWORDS:
            return False

        sql_injection_patterns = [
            r"\bor\s+1\s*=\s*1\b",
            r"\bunion\s+select\b",
            r"\bexec\s*\(",
            r"\bxp_cmdshell\b",
            r"\bsleep\s*\(",
            r"\bbenchmark\s*\(",
            r"\bwaitfor\s+delay\b",
        ]
        sql_injection_patterns.extend(rf"\b;\s*{re.escape(keyword)}\b" for keyword in cls.DANGEROUS_KEYWORDS)

        for pattern in sql_injection_patterns:
            if re.search(pattern, sql_upper, re.IGNORECASE):
                return False

        return True

    @classmethod
    def validate_timeout(cls, timeout: int) -> bool:
        """验证timeout参数"""
        return isinstance(timeout, int) and 1 <= timeout <= 600


def load_mysql_config() -> dict[str, Any]:
    config: dict[str, Any] = {
        "host": os.getenv("MYSQL_HOST"),
        "user": os.getenv("MYSQL_USER"),
        "password": os.getenv("MYSQL_PASSWORD"),
        "database": os.getenv("MYSQL_DATABASE"),
        "port": int(os.getenv("MYSQL_PORT") or "3306"),
        "charset": "utf8mb4",
        "description": os.getenv("MYSQL_DATABASE_DESCRIPTION") or "默认 MySQL 数据库",
    }

    required_keys = ["host", "user", "password", "database"]
    for key in required_keys:
        if not config[key]:
            raise MySQLConnectionError(
                f"MySQL configuration missing required key: {key}, please check your environment variables."
            )

    return config


def create_connection(config: dict[str, Any]) -> pymysql.Connection:
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return pymysql.connect(
                host=config["host"],
                user=config["user"],
                password=config["password"],
                database=config["database"],
                port=config["port"],
                charset=config.get("charset", "utf8mb4"),
                cursorclass=DictCursor,
                connect_timeout=10,
                read_timeout=60,
                write_timeout=30,
                autocommit=True,
            )
        except MySQLError as exc:
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
                continue
            raise ConnectionError(f"MySQL connection failed: {exc}") from exc

    raise ConnectionError("MySQL connection failed")


def execute_query_with_timeout(
    connection: pymysql.Connection, sql: str, params: tuple | None = None, timeout: int = 10
):
    """使用线程池实现超时控制，避免信号导致的生成器问题"""

    def query_worker():
        cursor = connection.cursor(DictCursor)
        try:
            if params is None:
                cursor.execute(sql)
            else:
                cursor.execute(sql, params)
            return cursor.fetchall()
        finally:
            cursor.close()

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(query_worker)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        try:
            connection.close()
        except Exception:
            pass
        raise QueryTimeoutError(f"Query timeout after {timeout} seconds") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def limit_result_size(result: list, max_chars: int = 10000) -> list:
    """按字符预算截取展示行，不修改完整查询结果。"""
    limited_result = []
    current_chars = 0
    for row in result:
        row_chars = len(str(row))
        if current_chars + row_chars > max_chars:
            break
        limited_result.append(row)
        current_chars += row_chars
    return limited_result


def format_query_result(result: list[dict[str, Any]]) -> str:
    """输出真实行数和预览截断状态，避免把未展示误报为无数据。"""
    limited_result = limit_result_size(result[:50])
    truncated = len(limited_result) < len(result)
    result_str = json.dumps(
        {"row_count": len(result), "displayed_row_count": len(limited_result), "truncated": truncated}
    )
    if not result:
        return result_str + "\n\n查询执行成功，但没有返回任何结果"

    if limited_result:
        columns = list(limited_result[0].keys())

        col_widths = {}
        for col in columns:
            col_widths[col] = max(len(str(col)), max(len(str(row.get(col, ""))) for row in limited_result))
            col_widths[col] = min(col_widths[col], 50)

        header = "| " + " | ".join(f"{col:<{col_widths[col]}}" for col in columns) + " |"
        separator = "|" + "|".join("-" * (col_widths[col] + 2) for col in columns) + "|"

        rows = []
        for row in limited_result:
            row_str = "| " + " | ".join(f"{str(row.get(col, '')):<{col_widths[col]}}" for col in columns) + " |"
            rows.append(row_str)

        result_str += f"\n\n查询结果（共 {len(result)} 行，展示 {len(limited_result)} 行）:\n\n"
        result_str += header + "\n" + separator + "\n"
        result_str += "\n".join(rows)
    if truncated:
        result_str += (
            "\n\n预览已截断，不能用于全量汇总或判断无数据。"
            "需要完整明细时使用 --output-json 导出；汇总指标应由 SQL 对完整范围聚合。"
        )
    return result_str


def run_query(sql: str, timeout: int, output_json: str | None = None) -> str:
    """查询后按需导出全部明细，stdout 仅提供带截断标志的预览。"""
    if not MySQLSecurityChecker.validate_sql(sql):
        raise ValueError("SQL语句包含不安全的操作或可能的注入攻击，请检查SQL语句")

    if not MySQLSecurityChecker.validate_timeout(timeout):
        raise ValueError("timeout参数必须在1-600之间")

    config = load_mysql_config()
    connection = create_connection(config)
    try:
        result = execute_query_with_timeout(connection, sql, timeout=timeout or 60)
        if output_json:
            # 独占创建防止覆盖旧结果或跟随同名符号链接；父目录由 PI 准备。
            with Path(output_json).open("x", encoding="utf-8") as output:
                json.dump(
                    {"row_count": len(result), "truncated": False, "rows": result},
                    output,
                    ensure_ascii=False,
                    default=str,
                )
                output.write("\n")
        preview = format_query_result(result)
        return preview + (f"\n\n完整结果已导出：{output_json}" if output_json else "")
    finally:
        if connection.open:
            connection.close()


def build_query_error(exc: Exception, sql: str) -> str:
    error_msg = f"SQL查询执行失败: {exc}\n\n{sql}"

    if "timeout" in str(exc).lower():
        error_msg += "\n\n💡 建议：查询超时了，请尝试以下方法：\n"
        error_msg += "1. 减少查询的数据量（使用WHERE条件过滤）\n"
        error_msg += "2. 使用LIMIT子句限制返回行数\n"
        error_msg += "3. 增加timeout参数值（最大600秒）"
    elif "table" in str(exc).lower() and "doesn't exist" in str(exc).lower():
        error_msg += "\n\n💡 建议：表不存在，请使用 scripts/list_tables.py 查看可用的表名"
    elif "column" in str(exc).lower() and "doesn't exist" in str(exc).lower():
        error_msg += "\n\n💡 建议：列不存在，请使用 scripts/describe_table.py 查看表结构"
    elif "not enough arguments for format string" in str(exc).lower():
        error_msg += (
            "\n\n💡 建议：SQL 中的百分号 (%) 被当作参数占位符使用。"
            " 如需匹配包含百分号的文本，请将百分号写成双百分号 (%%) 或使用参数化查询。"
        )

    return error_msg


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行只读 MySQL SQL 查询")
    parser.add_argument("--sql", required=True, help="要执行的SQL查询语句")
    parser.add_argument("--timeout", type=int, default=60, help="查询超时时间（秒），默认60秒，最大600秒")
    parser.add_argument("--output-json", help="完整结果 JSON 路径，父目录须已存在，不覆盖已有文件")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        print(run_query(args.sql, args.timeout, args.output_json))
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(build_query_error(exc, args.sql), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
