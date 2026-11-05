"""六种真实 Office 文件经过现有四类预览入口后的内容与原文件契约。"""

import os
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from test.integration.api.helpers import create_default_agent_thread, delete_created_threads, get_thread_workdir_path
from yuxi.infrastructure.minio import get_minio_client
from yuxi.modules.knowledge.cache import KNOWLEDGE_BASE_CACHE_KEY_PREFIX
from yuxi.modules.knowledge.models import KnowledgeBase, KnowledgeFile

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
FIXTURES = Path(__file__).parents[2] / "fixtures/office"


def assert_preview(response, suffix):
    """核对内容 oracle，拒绝仅 HTTP 200 或文件签名作为成功。"""
    assert response.status_code == 200, response.text[:300]
    if suffix in {"xls", "xlsx"}:
        payload = response.json()
        assert payload["preview_type"] == "spreadsheet"
        assert payload["supported"] is True
        sheets = payload["content"]["sheets"]
        assert [sheet["name"] for sheet in sheets] == ["销售", "说明"]
        assert sheets[0]["rows"][0][0]["colspan"] == 2
        assert sheets[0]["rows"][1][0]["text"] == "25.00%"
        assert sheets[1]["rows"][0][0]["text"] == "第二张表"
    else:
        import pypdfium2

        assert response.headers["content-type"].startswith("application/pdf")
        with pypdfium2.PdfDocument(response.content) as pdf:
            text = ""
            for index in range(len(pdf)):
                with closing(pdf[index]) as page, closing(page.get_textpage()) as textpage:
                    text += textpage.get_text_range()
        assert "Officepreviewverification42" in "".join(text.split())


@pytest.mark.parametrize("suffix", ["doc", "docx", "ppt", "pptx", "xls", "xlsx"])
async def test_office_preview_across_all_existing_entries(test_client, admin_headers, suffix):
    """真实 HTTP、PostgreSQL 与 MinIO 验证四入口并保留原文件字节。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"], poolclass=NullPool)
    sessions = async_sessionmaker(engine)
    identity = uuid4().hex
    filename = f"pytest-office-{identity}.{suffix}"
    content = (FIXTURES / f"readonly.{suffix}").read_bytes()
    workspace_path = f"/{filename}"
    thread_id = await create_default_agent_thread(test_client, admin_headers, title_prefix="office-preview")
    profile = await test_client.get("/api/auth/me", headers=admin_headers)
    assert profile.status_code == 200, profile.text
    thread = {"id": thread_id, "uid": profile.json()["uid"], "workdir_path": await get_thread_workdir_path(thread_id)}
    kb_id, file_id = f"pytest-office-{identity}", identity
    minio = get_minio_client()
    bucket = minio.KB_BUCKETS["documents"]
    object_name = f"{kb_id}/{filename}"
    try:
        upload = await test_client.post(
            "/api/workspace/upload",
            headers=admin_headers,
            data={"parent_path": "/"},
            files={"files": (filename, content)},
        )
        assert upload.status_code == 200, upload.text
        result = await test_client.get("/api/workspace/file", headers=admin_headers, params={"path": workspace_path})
        assert_preview(result, suffix)
        original = await test_client.get("/api/workspace/download", headers=admin_headers, params={"path": workspace_path})
        assert original.content == content
        anonymous = await test_client.get("/api/workspace/file", params={"path": workspace_path})
        assert anonymous.status_code in {401, 403}

        upload = await test_client.post(
            "/api/viewer/filesystem/upload",
            headers=admin_headers,
            data={"parent_path": "/", "thread_id": thread["id"]},
            files={"files": (filename, content)},
        )
        assert upload.status_code == 200, upload.text
        result = await test_client.get(
            "/api/viewer/filesystem/file",
            headers=admin_headers,
            params={"thread_id": thread["id"], "path": workspace_path},
        )
        assert_preview(result, suffix)
        artifact_url = upload.json()["entries"][0]["artifact_url"]
        result = await test_client.get(artifact_url, headers=admin_headers, params={"preview": "true"})
        assert_preview(result, suffix)
        original = await test_client.get(artifact_url, headers=admin_headers, params={"download": "true"})
        assert original.content == content

        # 只创建预览所需的原文元数据，不触发无关的模型/向量索引流程。
        async with sessions.begin() as session:
            session.add(KnowledgeBase(kb_id=kb_id, name=filename, kb_type="milvus", created_by=thread["uid"]))
            await session.flush()
            session.add(
                KnowledgeFile(
                    kb_id=kb_id,
                    file_id=file_id,
                    filename=filename,
                    file_size=len(content),
                    minio_url=f"minio://{bucket}/{object_name}",
                    created_by=thread["uid"],
                )
            )
        await minio.aupload_file(bucket_name=bucket, object_name=object_name, data=content)
        result = await test_client.get("/api/workspace/knowledge/file", headers=admin_headers, params={"kb_id": kb_id, "file_id": file_id})
        assert_preview(result, suffix)
        assert await minio.adownload_file(bucket, object_name) == content
    finally:
        await test_client.delete("/api/workspace/file", headers=admin_headers, params={"path": workspace_path})
        await test_client.delete(
            "/api/viewer/filesystem/file",
            headers=admin_headers,
            params={"thread_id": thread["id"], "path": workspace_path},
        )
        await delete_created_threads(thread["id"])
        async with Redis.from_url(os.environ["REDIS_URL"]) as redis:
            await redis.delete(f"{KNOWLEDGE_BASE_CACHE_KEY_PREFIX}{kb_id}")
        async with sessions.begin() as session:
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))
        await minio.adelete_objects_by_prefix(bucket, f"{kb_id}/")
        await engine.dispose()
