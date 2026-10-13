"""检索来源经过真实消息仓储后保持 JSON 内容与会话归属。"""

import json
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from yuxi.knowledge.utils import strip_nul_characters
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.services import chat_service
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import AgentRun, Conversation, Message, Project, User


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """回滚事务不创建沙盒。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """回滚事务不创建知识库资源。"""
    yield


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


@pytest.mark.asyncio
@pytest.mark.parametrize("model_audit", [False, True])
async def test_knowledge_sources_round_trip_through_message_repository(model_audit):
    """回读 PostgreSQL JSON 列，验证来源属于本次会话的 assistant 消息。"""
    uid = "pytest-source-" + uuid.uuid4().hex
    project_id = str(uuid.uuid4())
    sources = strip_nul_characters(
        [
            {
                "kb_id": "kb-test",
                "file_id": "file-test",
                "content": "证\x00据",
                "metadata": {
                    "source_ref": {"title": "示例\x00文章", "author": "示例作者", "source_type": "knowledge_base"}
                },
            }
        ]
    )
    pg_manager.initialize()
    try:
        async with pg_manager.get_async_session_context() as db:
            db.add(User(username=uid, uid=uid, password_hash="test-only"))
            await db.flush()
            db.add(
                Project(
                    id=project_id,
                    uid=uid,
                    selection_status="implicit",
                    workdir_path=f"projects/{project_id}",
                    directory_mode="managed",
                )
            )
            await db.flush()
            conversation = Conversation(thread_id=uid, uid=uid, project_id=project_id, agent_id="main", status="active")
            db.add(conversation)
            await db.flush()
            repo = ConversationRepository(db)
            if model_audit:
                run_id = str(uuid.uuid4())
                db.add(
                    AgentRun(
                        id=run_id,
                        conversation_id=conversation.id,
                        conversation_thread_id=uid,
                        runtime_scope_id=uid,
                        agent_slug="main",
                        uid=uid,
                        request_id=uid,
                        status="running",
                        source="web",
                        channel="web",
                        run_type="chat",
                        input_payload={},
                    )
                )
                await db.flush()
                db.add(
                    Message(
                        conversation_id=conversation.id,
                        run_id=run_id,
                        request_id=uid,
                        role="assistant",
                        message_type="model_audit",
                        operation_id="model-current",
                        execution_status="running",
                        content="",
                        extra_metadata={},
                    )
                )
                await db.flush()
                await chat_service._reconcile_model_audit_message(
                    repo,
                    run_id=run_id,
                    operation_id="model-current",
                    msg_dict={"content": "回答"},
                    trace_info=None,
                    additional_metadata={"knowledge_sources": sources},
                )
            else:
                await chat_service._save_ai_message(
                    repo,
                    uid,
                    {"content": "回答"},
                    additional_metadata={"knowledge_sources": sources},
                    commit=False,
                )
            row = (
                await db.execute(
                    select(Message.content, Message.extra_metadata).where(Message.conversation_id == conversation.id)
                )
            ).one()
            assert row.content == "回答"
            assert row.extra_metadata["knowledge_sources"] == sources
            extracted = (
                await db.execute(
                    select(
                        Message.extra_metadata["knowledge_sources"][0]["content"].as_string(),
                        Message.extra_metadata["knowledge_sources"][0]["metadata"]["source_ref"]["title"].as_string(),
                        Message.extra_metadata["state_reconciled"].as_boolean(),
                    ).where(Message.conversation_id == conversation.id)
                )
            ).one()
            assert extracted[0:2] == ("证据", "示例文章")
            if model_audit:
                assert extracted[2] is True
            await db.rollback()
    finally:
        await pg_manager.close()
