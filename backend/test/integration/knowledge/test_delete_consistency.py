"""真实删除 HTTP 与独立存储回读；仅由 run_delete.sh 在一次性空环境运行。"""

import io
import os
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import httpx
import pytest
from neo4j import GraphDatabase
from neo4j.exceptions import AuthError
from pymilvus import utility
from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService
from yuxi.knowledge.graphs.milvus_graph_vector_store import MilvusGraphVectorStore
from yuxi.knowledge.implementations.milvus import CONTENT_SPARSE_FIELD, MilvusKB
from yuxi.storage.minio import get_minio_client
from yuxi.storage.neo4j import get_shared_neo4j_connection, safe_neo4j_label
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Role, RolePermission, User, UserRoleAssignment
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeFile,
    KnowledgeGraphEntity,
    KnowledgeGraphEntityMention,
    KnowledgeGraphTriple,
    KnowledgeGraphTripleMention,
)
from yuxi.utils.auth_utils import AuthUtils


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_failure_retry_folder_and_graph_intermediate_state():
    """正常、存储故障、重试和图中间态均以实际存储内容为准。"""
    base_url = os.getenv("TEST_KB_BASE_URL")
    if not base_url:
        pytest.skip("需要 run_delete.sh 的一次性 HTTP 与全部存储")
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    pg_manager.initialize()
    suffix = uuid4().hex[:12]
    kb_id, other_kb = f"kb_delete_{suffix}", f"kb_other_{suffix}"
    label = safe_neo4j_label(kb_id)
    uid = f"delete_{suffix}"
    password = "Synthetic-delete-only-123!"
    store = get_minio_client()
    graph = get_shared_neo4j_connection()
    executor = MilvusKB(work_dir="/state/knowledge")
    files = {
        name: f"{name}_{suffix}"
        for name in ("healthy", "faulty", "object_fault", "orphan", "folder", "child", "graph", "remaining", "other")
    }
    objects = {}
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as api:
        try:
            assert await conn.fetchval("SELECT count(*) FROM users") == 0, "只允许一次性空库"
            response = await api.post("/api/auth/initialize", json={"uid": uid, "password": password})
            assert response.status_code == 200, response.text
            response = await api.post("/api/auth/token", data={"username": uid, "password": password})
            assert response.status_code == 200, response.text
            api.headers["Authorization"] = "Bearer " + response.json()["access_token"]
            async with pg_manager.get_async_session_context() as db:
                db.add_all(
                    [KnowledgeBase(kb_id=k, name=k, kb_type="milvus", created_by=uid) for k in (kb_id, other_kb)]
                )
                await db.flush()
                db.add(KnowledgeFile(file_id=files["folder"], kb_id=kb_id, filename="folder", is_folder=True))
                await db.flush()
                for name, file_id in files.items():
                    if name == "folder":
                        continue
                    owner = other_kb if name == "other" else kb_id
                    paths = [
                        f"{owner}/upload/{file_id}.txt",
                        f"{owner}/parsed/{file_id}.md",
                        f"{owner}/preview/{file_id}.pdf",
                    ]
                    objects[file_id] = paths
                    store.ensure_bucket_exists(store.KB_BUCKETS["documents"])
                    for path in paths:
                        store.client.put_object(store.KB_BUCKETS["documents"], path, io.BytesIO(b"sentinel"), 8)
                    db.add(
                        KnowledgeFile(
                            file_id=file_id,
                            kb_id=owner,
                            filename=name + ".txt",
                            file_type="txt",
                            status="indexed",
                            path=f"minio://{store.KB_BUCKETS['documents']}/{paths[0]}",
                            parent_id=files["folder"] if name == "child" else None,
                            chunk_count=1,
                        )
                    )
                    await db.flush()
                    is_graph = name in {"graph", "remaining"}
                    db.add(
                        KnowledgeChunk(
                            chunk_id=file_id,
                            file_id=file_id,
                            kb_id=owner,
                            chunk_index=0,
                            content="sentinel searchable content",
                            graph_structure_indexed=name == "remaining",
                            graph_indexed=False,
                        )
                    )
                    await db.flush()
                    if is_graph:
                        entity_id = "entity_" + file_id
                        db.add(
                            KnowledgeGraphEntity(
                                entity_id=entity_id, kb_id=owner, name=name, normalized_name=name, label="test"
                            )
                        )
                        await db.flush()
                        db.add(
                            KnowledgeGraphEntityMention(
                                entity_id=entity_id, file_id=file_id, chunk_id=file_id, kb_id=owner
                            )
                        )
                        with graph.driver.session() as session:
                            session.run(
                                f"CREATE (c:Chunk:MilvusKB:`{label}` {{kb_id:$kb,file_id:$file}}), "
                                f"(e:Entity:MilvusKB:`{label}` {{kb_id:$kb,name:$file}}), (c)-[:MENTIONS]->(e)",
                                kb=owner,
                                file=file_id,
                            ).consume()
            shared_id = "shared_" + suffix
            async with pg_manager.get_async_session_context() as db:
                db.add(
                    KnowledgeGraphEntity(
                        entity_id=shared_id, kb_id=kb_id, name="shared", normalized_name="shared", label="test"
                    )
                )
                await db.flush()
                for name in ("graph", "remaining"):
                    fid = files[name]
                    db.add(KnowledgeGraphEntityMention(entity_id=shared_id, kb_id=kb_id, file_id=fid, chunk_id=fid))
                    db.add(
                        KnowledgeGraphTriple(
                            triple_id="triple_" + fid,
                            kb_id=kb_id,
                            source_entity_id="entity_" + fid,
                            target_entity_id=shared_id,
                            relation_type="test",
                            content="sentinel relation",
                        )
                    )
                    await db.flush()
                    db.add(
                        KnowledgeGraphTripleMention(triple_id="triple_" + fid, kb_id=kb_id, file_id=fid, chunk_id=fid)
                    )
                    with graph.driver.session() as session:
                        session.run(
                            f"MATCH (c:Chunk:MilvusKB:`{label}` {{file_id:$file}})-[:MENTIONS]->(e) "
                            f"MERGE (s:Entity:MilvusKB:`{label}` {{kb_id:$kb,name:'shared'}}) "
                            "CREATE (c)-[:MENTIONS]->(s), (e)-[:RELATION {kb_id:$kb,file_id:$file}]->(s)",
                            kb=kb_id,
                            file=fid,
                        ).consume()
            # Neo4j 写入先于 PostgreSQL 标记/引用，构造该真实中间态。
            with graph.driver.session() as session:
                session.run(
                    f"CREATE (c:Chunk:MilvusKB:`{label}` {{kb_id:$kb,file_id:$file}}), "
                    f"(e:Entity:MilvusKB:`{label}` {{kb_id:$kb,name:$file}}), (c)-[:MENTIONS]->(e)",
                    kb=kb_id,
                    file=files["orphan"],
                ).consume()
            graph_vectors = MilvusGraphVectorStore()
            info = SimpleNamespace(dimension=2, model_id="synthetic")
            entities = graph_vectors._get_or_create_entity_collection(kb_id, info)
            triples = graph_vectors._get_or_create_triple_collection(kb_id, info)
            entities.insert(
                [
                    dict(id=eid, content="sentinel entity", embedding=[0.1, 0.2])
                    for eid in [shared_id, "entity_" + files["graph"], "entity_" + files["remaining"]]
                ]
            )
            triples.insert(
                [
                    dict(
                        id="triple_" + files[name],
                        content="sentinel relation",
                        source_id="entity_" + files[name],
                        target_id=shared_id,
                        embedding=[0.1, 0.2],
                    )
                    for name in ("graph", "remaining")
                ]
            )
            for index in (entities, triples):
                index.flush()
                index.load()

            collection = executor._create_new_collection(
                kb_id, SimpleNamespace(dimension=2, model_id="synthetic"), kb_id
            )
            rows = [
                dict(
                    id=fid,
                    chunk_id=fid,
                    file_id=fid,
                    chunk_index=0,
                    content="sentinel searchable content",
                    embedding=[0.1, 0.2],
                )
                for name, fid in files.items()
                if name not in {"folder", "other"}
            ]
            collection.insert(rows)
            collection.flush()
            collection.load()

            def vector_count(file_id):
                """独立查询实体与真实 BM25 返回，避免使用删除函数的返回值。"""
                expression = f'file_id == "{file_id}"'
                vectors = collection.query(expr=expression, output_fields=["id"], consistency_level="Strong")
                hits = collection.search(
                    ["sentinel"],
                    CONTENT_SPARSE_FIELD,
                    {"metric_type": "BM25"},
                    limit=10,
                    expr=expression,
                    consistency_level="Strong",
                )
                assert len(vectors) == len(hits[0])
                return len(vectors)

            async def assert_deleted(name):
                """读取数据库、向量/关键词索引、文件对象和图节点的最终事实。"""
                file_id = files[name]
                assert await conn.fetchval("SELECT count(*) FROM knowledge_files WHERE file_id=$1", file_id) == 0
                assert await conn.fetchval("SELECT count(*) FROM knowledge_chunks WHERE file_id=$1", file_id) == 0
                assert vector_count(file_id) == 0
                assert all(not store.file_exists(store.KB_BUCKETS["documents"], path) for path in objects[file_id])
                with graph.driver.session() as session:
                    assert (
                        session.run(
                            f"MATCH (c:Chunk:MilvusKB:`{label}` {{file_id:$file}}) RETURN count(c)", file=file_id
                        ).single()[0]
                        == 0
                    )

            for name in ("healthy", "faulty", "child", "graph", "remaining"):
                assert vector_count(files[name]) == 1
            async with pg_manager.get_async_session_context() as db:
                role = Role(code=f"reader-{suffix}", name="删除边界合成读者", default_scope_type="self")
                role.permissions = [RolePermission(permission_key="knowledge_base:read")]
                reader = User(
                    uid=f"reader_{suffix}", username=f"reader_{suffix}", password_hash=AuthUtils.hash_password(password)
                )
                db.add_all([role, reader])
                await db.flush()
                db.add(UserRoleAssignment(user_id=reader.id, role_id=role.id, scope_mode="inherit"))
            response = await api.post("/api/auth/token", data={"username": f"reader_{suffix}", "password": password})
            assert response.status_code == 200, response.text
            denied = await api.delete(
                f"/api/knowledge/databases/{kb_id}/documents/{files['healthy']}",
                headers={"Authorization": "Bearer " + response.json()["access_token"]},
            )
            assert denied.status_code in {403, 404}
            assert vector_count(files["healthy"]) == 1

            wrong_scope = await api.delete(f"/api/knowledge/databases/{other_kb}/documents/{files['healthy']}")
            assert wrong_scope.status_code >= 400
            assert vector_count(files["healthy"]) == 1

            response = await api.delete(f"/api/knowledge/databases/{kb_id}/documents/{files['healthy']}")
            assert response.status_code == 200, response.text
            await assert_deleted("healthy")

            # 缺失存储桶由真实 MinIO 返回错误；恢复原路径后重试。
            await conn.execute(
                "UPDATE knowledge_files SET path=$2 WHERE file_id=$1",
                files["object_fault"],
                "minio://missing-synthetic-bucket/sentinel.txt",
            )
            response = await api.delete(f"/api/knowledge/databases/{kb_id}/documents/{files['object_fault']}")
            assert response.status_code >= 400, response.text
            assert (
                await conn.fetchval("SELECT count(*) FROM knowledge_files WHERE file_id=$1", files["object_fault"]) == 1
            )
            assert vector_count(files["object_fault"]) == 1
            assert all(
                store.file_exists(store.KB_BUCKETS["documents"], path) for path in objects[files["object_fault"]]
            )
            await conn.execute(
                "UPDATE knowledge_files SET path=$2 WHERE file_id=$1",
                files["object_fault"],
                f"minio://{store.KB_BUCKETS['documents']}/{objects[files['object_fault']][0]}",
            )
            response = await api.delete(f"/api/knowledge/databases/{kb_id}/documents/{files['object_fault']}")
            assert response.status_code == 200, response.text
            await assert_deleted("object_fault")

            collection.release()
            response = await api.delete(f"/api/knowledge/databases/{kb_id}/documents/{files['faulty']}")
            assert response.status_code >= 400, response.text
            assert await conn.fetchval("SELECT count(*) FROM knowledge_files WHERE file_id=$1", files["faulty"]) == 1
            assert await conn.fetchval("SELECT count(*) FROM knowledge_chunks WHERE file_id=$1", files["faulty"]) == 1
            collection.load()
            assert vector_count(files["faulty"]) == 1
            response = await api.delete(f"/api/knowledge/databases/{kb_id}/documents/{files['faulty']}")
            assert response.status_code == 200, response.text
            await assert_deleted("faulty")

            response = await api.delete(f"/api/knowledge/databases/{kb_id}/documents/{files['folder']}")
            assert response.status_code == 200, response.text
            await assert_deleted("child")
            assert await conn.fetchval("SELECT count(*) FROM knowledge_files WHERE file_id=$1", files["folder"]) == 0

            bad_driver = GraphDatabase.driver(os.environ["NEO4J_URI"], auth=("neo4j", "wrong-synthetic-password"))
            try:
                service = MilvusGraphService(neo4j_connection=SimpleNamespace(driver=bad_driver))
                with pytest.raises(AuthError):
                    await service.delete_file_graph(kb_id, files["graph"])
            finally:
                bad_driver.close()
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM knowledge_graph_entity_mentions WHERE file_id=$1", files["graph"]
                )
                == 2
            )
            response = await api.delete(f"/api/knowledge/databases/{kb_id}/documents/{files['graph']}")
            assert response.status_code == 200, response.text
            await assert_deleted("graph")
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM knowledge_graph_entities WHERE entity_id=$1", "entity_" + files["graph"]
                )
                == 0
            )

            assert {
                row["id"] for row in entities.query(expr='id != ""', output_fields=["id"], consistency_level="Strong")
            } == {shared_id, "entity_" + files["remaining"]}
            assert {
                row["id"] for row in triples.query(expr='id != ""', output_fields=["id"], consistency_level="Strong")
            } == {"triple_" + files["remaining"]}
            response = await api.delete(f"/api/knowledge/databases/{kb_id}/documents/{files['orphan']}")
            assert response.status_code == 200, response.text
            await assert_deleted("orphan")

            repeated = await api.delete(f"/api/knowledge/databases/{kb_id}/documents/{files['graph']}")
            assert repeated.status_code in {200, 400, 404}
            assert vector_count(files["remaining"]) == 1
            response = await api.delete(f"/api/knowledge/databases/{kb_id}")
            assert response.status_code == 200, response.text
            assert await conn.fetchval("SELECT count(*) FROM knowledge_bases WHERE kb_id=$1", kb_id) == 0
            assert not utility.has_collection(kb_id, using=executor.connection_alias)
            assert not utility.has_collection(entities.name, using=graph_vectors.connection_alias)
            assert not utility.has_collection(triples.name, using=graph_vectors.connection_alias)
            assert not list(
                store.client.list_objects(store.KB_BUCKETS["documents"], prefix=f"{kb_id}/", recursive=True)
            )
            with graph.driver.session() as session:
                assert session.run(f"MATCH (n:MilvusKB:`{label}`) RETURN count(n)").single()[0] == 0
            assert await conn.fetchval("SELECT count(*) FROM knowledge_files WHERE file_id=$1", files["other"]) == 1
            assert all(store.file_exists(store.KB_BUCKETS["documents"], path) for path in objects[files["other"]])
        finally:
            await conn.close()
            await pg_manager.close()
            graph.driver.close()
