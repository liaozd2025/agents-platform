"""检索来源经过真实消息仓储后保持 JSON 内容与会话归属。"""

import uuid

import pytest
from sqlalchemy import select

from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.services import chat_service
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Conversation, Message, Project, User


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """回滚事务不创建沙盒。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """回滚事务不创建知识库资源。"""
    yield


@pytest.mark.asyncio
async def test_knowledge_sources_round_trip_through_message_repository():
    """回读 PostgreSQL JSON 列，验证来源属于本次会话的 assistant 消息。"""
    uid = "pytest-source-" + uuid.uuid4().hex
    project_id = str(uuid.uuid4())
    sources = [
        {
            "kb_id": "kb-test",
            "file_id": "file-test",
            "content": "证据",
            "metadata": {"source_ref": {"title": "示例文章", "author": "示例作者", "source_type": "knowledge_base"}},
        }
    ]
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
            await chat_service._save_ai_message(
                ConversationRepository(db),
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
            await db.rollback()
    finally:
        await pg_manager.close()
