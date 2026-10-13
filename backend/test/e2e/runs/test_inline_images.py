"""真实 API/worker/PostgreSQL/文件与模型协议验证内联多图和视觉转述。"""

import asyncio
import base64
import json
import os
import io

from PIL import Image
from yuxi.workspace.paths import user_workdir_host_dir
import uuid
from pathlib import Path

import asyncpg
import httpx
import pytest


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_inline_images_persist_and_reach_direct_or_transcribed_model():
    """直传、声明预路由和拒绝后转述均保留两张图的文件及输入路径。"""
    if os.getenv("INLINE_IMAGES_E2E") != "1":
        pytest.skip("通过 run_inline_images.sh 使用一次性空库与合成模型")
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    try:
        assert await conn.fetchval("SELECT count(*) FROM users") == 0, "仅允许一次性空库"
        async with httpx.AsyncClient(base_url="http://api:5050", timeout=60) as api:
            response = await api.post(
                "/api/auth/initialize", json={"uid": "inline_images_test", "password": uuid.uuid4().hex}
            )
            response.raise_for_status()
            api.headers["Authorization"] = "Bearer " + response.json()["access_token"]

            async def post(path, body):
                """提交测试资源，返回真实协议结果。"""
                response = await api.post(path, json=body)
                response.raise_for_status()
                return response.json()

            await post(
                "/api/system/model-providers",
                {
                    "provider_id": "image-test",
                    "display_name": "内联图片测试",
                    "provider_type": "openai",
                    "base_url": "http://replay:8777/v1",
                    "api_key": "synthetic-only",
                    "capabilities": ["chat"],
                    "is_enabled": True,
                    "include_user_uid": True,
                    "enabled_models": [
                        {"id": name, "type": "chat", "source": "manual", "input_modalities": modalities}
                        for name, modalities in [
                            ("direct", ["text", "image"]),
                            ("text", ["text"]),
                            ("unknown", []),
                            ("vision", ["text", "image"]),
                        ]
                    ],
                },
            )
            await post("/api/system/config", {"key": "vision_model", "value": "image-test:vision"})
            await post(
                "/api/agent",
                {
                    "slug": "image-test",
                    "name": "内联图片测试",
                    "backend_id": "ChatbotAgent",
                    "config_json": {
                        "context": {
                            "model": "image-test:direct",
                            "tools": [],
                            "skills": [],
                            "mcps": [],
                            "knowledges": [],
                            "subagents": [],
                        }
                    },
                },
            )
            # 两个不同的已知 PNG payload；回读精确字节，不依赖模型自述证明文件存在。
            images = []
            for color in ["red", "blue"]:
                buffer = io.BytesIO()
                Image.new("RGB", (2, 2), color).save(buffer, format="PNG")
                images.append(buffer.getvalue())
            for model in ["direct", "text", "unknown"]:
                marker = "inline-images-" + model
                thread = await post("/api/chat/thread", {"agent_id": "image-test", "title": marker})
                submitted = await post(
                    "/api/agent/runs",
                    {
                        "agent_slug": "image-test",
                        "thread_id": thread["id"],
                        "query": marker,
                        "image_content": [base64.b64encode(data).decode() for data in images],
                        "model_spec": "image-test:" + model,
                    },
                )
                run_id = submitted["run_id"]
                for _ in range(600):
                    run = await conn.fetchrow("SELECT * FROM agent_runs WHERE id=$1", run_id)
                    if run["status"] in {"completed", "failed", "cancelled"} and not run["runtime_cleanup_pending"]:
                        break
                    await asyncio.sleep(0.1)
                assert run["status"] == "completed", (model, run["status"], run["error_message"])
                output = await conn.fetchrow(
                    "SELECT run_id,content FROM messages WHERE id=$1", run["output_message_id"]
                )
                assert output["run_id"] == run_id and output["content"] == "INLINE_IMAGES_DONE"
                project_path = thread["workdir_path"]
                names = [f"chat-image-{run['request_id']}.png", f"chat-image-{run['request_id']}-2.png"]
                for name, data in zip(names, images, strict=True):
                    assert (
                        user_workdir_host_dir("inline_images_test", project_path) / "uploads" / name
                    ).read_bytes() == data
                response = await api.get(f"/api/agent/runs/{run_id}/events")
                response.raise_for_status()
                events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                assert any(event.get("payload", {}).get("status") == "completed" for event in events)
                wire = json.loads(Path("/state/image-model-requests.json").read_text())
                assert all(request["test_signed_uid"] == "inline_images_test" for request in wire)
                selected = [request for request in wire if request["model"] == model]
                assert len(selected) == (2 if model == "unknown" else 1)
                main = selected[-1]
                user_blocks = next(
                    message["content"] for message in reversed(main["messages"]) if message["role"] == "user"
                )
                rendered = json.dumps(user_blocks, ensure_ascii=False)
                for name in names:
                    assert name in rendered
                for request in selected:
                    received = [
                        block
                        for message in request["messages"]
                        if isinstance(message.get("content"), list)
                        for block in message["content"]
                        if block["type"] == "image_url"
                    ]
                    if received:
                        assert [
                            base64.b64decode(block["image_url"]["url"].split(",", 1)[1]) for block in received
                        ] == images
                main_images = [block for block in user_blocks if block["type"] == "image_url"]
                if model == "direct":
                    assert len(main_images) == 2 and "<image_transcript" not in rendered
                else:
                    assert not main_images and "<image_transcript source=" in rendered
                    sidecars = [
                        request for request in wire if request["model"] == "vision" and marker in json.dumps(request)
                    ]
                    assert len(sidecars) == 1
                    received = [
                        block for block in sidecars[0]["messages"][0]["content"] if block["type"] == "image_url"
                    ]
                    assert [
                        base64.b64decode(block["image_url"]["url"].split(",", 1)[1]) for block in received
                    ] == images
    finally:
        await conn.close()
