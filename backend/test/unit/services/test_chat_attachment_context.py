from langchain.messages import HumanMessage

from yuxi.modules.agents.services.execution import _with_attachment_context


def test_attachment_context_is_added_only_to_model_message():
    original = HumanMessage(content="请总结附件")

    model_message = _with_attachment_context(
        original,
        [
            {
                "file_name": "report.pdf",
                "path": "/home/user/workspace/projects/project-1/uploads/report.pdf",
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
        [{"file_name": "notes.md", "path": "/home/user/workspace/projects/project-1/uploads/notes.md"}],
    )

    assert model_message.content[:2] == original.content
    assert model_message.content[-1]["type"] == "text"
    assert "<attachment_context>" in model_message.content[-1]["text"]


def test_attachment_context_ignores_records_without_paths():
    original = HumanMessage(content="继续")

    assert _with_attachment_context(original, [{"file_name": "missing"}]) is original


async def test_inline_images_reach_model_with_distinct_persisted_paths(tmp_path, monkeypatch):
    """两张真实输入图片分别落盘，并在各自消息中保留路径和原始多模态内容。"""
    import base64

    from yuxi.modules.agents.services.execution import _with_inline_image_paths
    from yuxi.modules.workspace.filesystem import Workspace
    from yuxi.modules.workspace.paths import ensure_user_workspace
    from yuxi.modules.workspace.workdir import Workdir

    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path))
    ensure_user_workspace("inline-owner")
    workdir = Workdir("projects/inline", Workspace("inline-owner"))
    images = [b"\x89PNG\r\n\x1a\nfirst", b"\xff\xd8\xffsecond"]
    original = HumanMessage(
        content=[
            {"type": "text", "text": "比较两张图"},
            *[
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(content).decode()}}
                for content in images
            ],
        ]
    )
    enriched = await _with_inline_image_paths(original, workdir=workdir, message_id=7)
    repeated = await _with_inline_image_paths(original, workdir=workdir, message_id=7)
    assert enriched.content[:3] == original.content
    text = enriched.content[-1]["text"]
    assert "chat-image-7-1.png" in text and "chat-image-7-2.jpg" in text
    assert repeated.content == enriched.content
    assert workdir.read_file("/uploads/chat-image-7-1.png", 100) == images[0]
    assert workdir.read_file("/uploads/chat-image-7-2.jpg", 100) == images[1]
    assert len(workdir.list_directory("/uploads")) == 2
    other = await _with_inline_image_paths(HumanMessage("没有图片"), workdir=workdir, message_id=8)
    assert "<attachment_context>" not in other.content


async def test_unverified_inline_bytes_never_advertise_a_file(tmp_path, monkeypatch):
    """损坏base64及未知格式不产生虚构参考路径或工作区写入。"""
    from unittest.mock import Mock

    from yuxi.modules.agents.services.attachments import persist_inline_chat_image

    workdir = Mock()
    for image in ("not-base64!!", "cGxhaW4gdGV4dA=="):
        assert await persist_inline_chat_image(workdir=workdir, image_content=image, request_id="invalid") is None
    workdir.copy_file_from_stream.assert_not_called()


async def test_oversized_inline_image_never_writes_reference_file(monkeypatch):
    """有效图片签名不能绕过附件大小边界。"""
    import base64
    from unittest.mock import Mock

    from yuxi.modules.agents.services import attachments

    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_SIZE_BYTES", 8)
    workdir = Mock()
    image = base64.b64encode(b"\x89PNG\r\n\x1a\nextra").decode()
    assert await attachments.persist_inline_chat_image(workdir=workdir, image_content=image, request_id="oversized") is None
    workdir.copy_file_from_stream.assert_not_called()


async def test_reference_write_failure_preserves_original_multimodal_input():
    """可选参考文件写入失败时继续原始图片输入，不声称文件已发布。"""
    import base64
    from types import SimpleNamespace
    from unittest.mock import Mock

    from yuxi.modules.agents.services.execution import _with_inline_image_paths

    workdir = SimpleNamespace(copy_file_from_stream=Mock(side_effect=OSError("disk full")))
    original = HumanMessage(
        content=[
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode()}}
        ]
    )
    result = await _with_inline_image_paths(original, workdir=workdir, message_id=8)
    assert result is original
    assert "attachment_context" not in str(result.content)
    workdir.copy_file_from_stream.assert_called_once()
