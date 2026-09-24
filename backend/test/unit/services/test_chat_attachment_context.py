import base64
from pathlib import Path

import pytest
from langchain.messages import HumanMessage

from yuxi.services.attachment_service import persist_inline_chat_image
from yuxi.services.chat_service import _with_attachment_context

WORKDIR_RELATIVE_PATH = "projects/11111111-1111-4111-8111-111111111111"


class _FakeWorkdir:
    """只记录写入 scope 的最小 Workdir 替身。"""

    relative_path = WORKDIR_RELATIVE_PATH

    def __init__(self):
        self.files: dict[str, bytes] = {}

    def copy_file_from_path(self, scope: str, source_path: str, *, overwrite: bool = True):
        del overwrite
        self.files[scope] = Path(source_path).read_bytes()


def test_attachment_context_is_added_only_to_model_message():
    original = HumanMessage(content="请总结附件")

    model_message = _with_attachment_context(
        original,
        [
            {
                "file_name": "report.pdf",
                "path": "/home/gem/user-data/projects/project-1/uploads/report.pdf",
            }
        ],
    )

    assert original.content == "请总结附件"
    assert model_message.content.startswith("请总结附件\n\n<attachment_context>")
    assert "report.pdf" in model_message.content
    assert model_message.type == "human"


def test_attachment_context_preserves_multimodal_content_blocks():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}
    original = HumanMessage(content=[{"type": "text", "text": "比较图片"}, image])

    model_message = _with_attachment_context(
        original,
        [{"file_name": "notes.md", "path": "/home/gem/user-data/projects/project-1/uploads/notes.md"}],
    )

    assert model_message.content[:2] == original.content
    assert model_message.content[-1]["type"] == "text"
    assert "<attachment_context>" in model_message.content[-1]["text"]


def test_attachment_context_ignores_records_without_paths():
    original = HumanMessage(content="继续")

    assert _with_attachment_context(original, [{"file_name": "missing"}]) is original


@pytest.mark.asyncio
async def test_inline_chat_image_path_reaches_model_input():
    """聊天输入的内联图片落盘后，参考图路径必须出现在本轮模型输入中。"""
    workdir = _FakeWorkdir()
    record = await persist_inline_chat_image(
        workdir=workdir,
        image_content=base64.b64encode(b"\x89PNG\r\n\x1a\nfake-png-body").decode("utf-8"),
        request_id="req-1",
    )
    assert record is not None

    message = _with_attachment_context(HumanMessage(content="根据这张图片生成种草图"), [record])

    assert "chat-image-req-1.png" in message.content
    assert record["path"] in message.content


def test_model_input_has_no_reference_path_without_inline_image_record():
    """落盘被拒或没有内联图片时，模型输入不得出现参考图路径（原缺陷状态）。"""
    message = _with_attachment_context(HumanMessage(content="根据这张图片生成种草图"), [])

    assert "<attachment_context>" not in message.content
