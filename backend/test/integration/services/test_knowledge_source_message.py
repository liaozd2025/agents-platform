"""PostgreSQL JSON 字段提取的 NUL 拒绝与清理回归。

旧 Conversation knowledge_sources 写入协议已退役；同 Turn/Run 来源持久归属
由 test_turn_references.py 的实际消息仓储与 HTTP 用例覆盖。
"""

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from yuxi.infrastructure.postgres.manager import pg_manager
from yuxi.modules.knowledge.utils import strip_nul_characters

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


@pytest.mark.asyncio
async def test_postgres_json_text_extraction_requires_nul_cleanup():
    """恢复原始 NUL 错误，确认清理后历史字段提取成功。"""
    payload = {"state_reconciled": True, "knowledge_sources": [{"content": "证\x00据"}]}
    query = text("SELECT CAST(:payload AS JSON) ->> 'state_reconciled'")
    pg_manager.initialize()
    try:
        async with pg_manager.get_async_session_context() as db:
            with pytest.raises(DBAPIError) as error:
                await db.execute(query, {"payload": json.dumps(payload)})
            assert error.value.orig.sqlstate == "22P05"
            await db.rollback()

            result = await db.execute(query, {"payload": json.dumps(strip_nul_characters(payload))})
            assert result.scalar_one() == "true"
            await db.rollback()
    finally:
        await pg_manager.close()
