"""真实 HTTP/worker 验证末块失败、完整重试及取消后的统计与 owner。"""

import asyncio
import io
import json
import os
from uuid import uuid4

import asyncpg
import httpx
import pytest
from pymilvus import Collection, connections
from yuxi.knowledge.chunking.ragflow_like.nlp import count_tokens
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import KnowledgeBase, KnowledgeFile


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_index_partial_failure_retry_and_cancel():
    """201 块末块故障保留 200 块真实统计；重试与取消不留下 owner 或重复块。"""
    if not os.getenv("TEST_KB_BASE_URL"):
        pytest.skip("需要 run_index.sh 的一次性环境")
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    pg_manager.initialize()
    suffix = uuid4().hex[:10]
    uid, kb_id, other_kb, fid = (prefix + suffix for prefix in ("index_", "kb_index_", "kb_other_", "file_"))
    password = "Synthetic-index-only-123!"
    model_url = os.environ["TEST_KB_MODEL_URL"]
    connections.connect(alias="index_readback", uri=os.environ["MILVUS_URI"])
    store = get_minio_client()
    async with (
        httpx.AsyncClient(base_url=os.environ["TEST_KB_BASE_URL"], timeout=30) as api,
        httpx.AsyncClient(base_url=model_url, timeout=10) as model,
    ):
        try:
            assert await conn.fetchval("SELECT count(*) FROM users") == 0, "只允许一次性空库"
            response = await api.post("/api/auth/initialize", json={"uid": uid, "password": password})
            assert response.status_code == 200, response.text
            response = await api.post("/api/auth/token", data={"username": uid, "password": password})
            assert response.status_code == 200, response.text
            api.headers["Authorization"] = "Bearer " + response.json()["access_token"]
            response = await api.post(
                "/api/system/model-providers",
                json={
                    "provider_id": "index-synthetic",
                    "display_name": "本地索引模型",
                    "provider_type": "openai",
                    "base_url": model_url + "/v1",
                    "embedding_base_url": model_url + "/v1/embeddings",
                    "api_key": "synthetic-only",
                    "capabilities": ["embedding"],
                    "is_enabled": True,
                    "enabled_models": [
                        {
                            "id": "index",
                            "display_name": "index",
                            "type": "embedding",
                            "source": "manual",
                            "dimension": 2,
                            "batch_size": 40,
                        }
                    ],
                },
            )
            assert response.status_code == 200, response.text
            params = {"chunk_preset_id": "separator", "chunk_parser_config": {"delimiter": "\n"}}
            markdown = "\n".join(f"sentinel-{i:03d}" for i in range(201)).encode()
            bucket = store.KB_BUCKETS["parsed"]
            path = f"{kb_id}/parsed/{fid}.md"
            store.ensure_bucket_exists(bucket)
            store.client.put_object(bucket, path, io.BytesIO(markdown), len(markdown))
            async with pg_manager.get_async_session_context() as db:
                db.add_all(
                    [
                        KnowledgeBase(
                            kb_id=k,
                            name=k,
                            kb_type="milvus",
                            created_by=uid,
                            embedding_model_spec="index-synthetic:index",
                        )
                        for k in (kb_id, other_kb)
                    ]
                )
                await db.flush()
                db.add(
                    KnowledgeFile(
                        file_id=fid,
                        kb_id=kb_id,
                        filename="sentinel.txt",
                        file_type="txt",
                        status="parsed",
                        markdown_file=f"minio://{bucket}/{path}",
                    )
                )

            async def enqueue(target=kb_id, pending=False):
                """通过产品 HTTP 提交实际 worker 任务。"""
                endpoint = "index-pending" if pending else "index"
                payload = {"params": params} if pending else {"file_ids": [fid], "params": params}
                result = await api.post(f"/api/knowledge/databases/{target}/documents/{endpoint}", json=payload)
                assert result.status_code == 200, result.text
                return result.json()["task_id"]

            async def terminal(task_id):
                """直接回读 Task 终态，不以接口 200 作为业务成功。"""
                for _ in range(600):
                    task = await conn.fetchrow("SELECT status, result, error FROM tasks WHERE id=$1", task_id)
                    if task["status"] in {"success", "failed", "cancelled"}:
                        return task
                    await asyncio.sleep(0.1)
                pytest.fail(f"Task 未收敛: {dict(task)}")

            async def check_file(count, status):
                """独立读取 PG 明细、文件统计、Milvus 主键与内容，并核对 owner 清空。"""
                file = await conn.fetchrow("SELECT * FROM knowledge_files WHERE file_id=$1", fid)
                chunks = await conn.fetch("SELECT chunk_id, content FROM knowledge_chunks WHERE file_id=$1", fid)
                vectors = Collection(kb_id, using="index_readback").query(
                    expr=f'file_id == "{fid}"', output_fields=["id", "content"], consistency_level="Strong"
                )
                assert len(chunks) == len(vectors) == file["chunk_count"] == count
                assert {row["content"] for row in chunks} == {f"sentinel-{i:03d}" for i in range(count)}
                assert {row["content"] for row in vectors} == {row["content"] for row in chunks}
                assert len({row["id"] for row in vectors}) == count
                assert file["status"] == status
                assert file["processing_task_id"] is file["processing_owner"] is None
                assert file["token_count"] == sum(count_tokens(f"sentinel-{i:03d}") for i in range(count))

            await model.post("/control/reset", json={"mode": "fail"})
            task = await terminal(await enqueue())
            assert task["status"] == "failed", dict(task)
            result = json.loads(task["result"]) if isinstance(task["result"], str) else task["result"]
            assert result["failed"] == 1 and result["processed"] == 1
            await check_file(200, "error_indexing")

            # 待处理入口同样报告失败，而不是 result.failed=1 的 success。
            task = await terminal(await enqueue(pending=True))
            assert task["status"] == "failed", dict(task)
            await check_file(200, "error_indexing")
            await model.post("/control/reset", json={"mode": "healthy"})
            for _ in range(2):
                assert (await terminal(await enqueue()))["status"] == "success"
                await check_file(201, "indexed")

            task = await terminal(await enqueue(target=other_kb))
            assert task["status"] == "failed", dict(task)
            await check_file(201, "indexed")

            await model.post("/control/reset", json={"mode": "block"})
            task_id = await enqueue()
            for _ in range(300):
                if (await model.get("/control")).json()["ready"]:
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("worker 未到达末块屏障")
            response = await api.post(f"/api/tasks/{task_id}/cancel")
            assert response.status_code == 200, response.text
            assert (await terminal(task_id))["status"] == "cancelled"
            await check_file(200, "error_indexing")
            await model.post("/control/release", json={})
            await model.post("/control/reset", json={"mode": "healthy"})
            assert (await terminal(await enqueue()))["status"] == "success"
            await check_file(201, "indexed")
        finally:
            await model.post("/control/release", json={})
            connections.disconnect("index_readback")
            await conn.close()
            await pg_manager.close()
