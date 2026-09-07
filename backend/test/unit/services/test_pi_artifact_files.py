"""PI 交付文件通过真实 Workdir 边界回读。"""

import hashlib
import json
import os

import pytest

from yuxi.services import pi_execution_service
from yuxi.services.pi_execution_service import LocalPiAdapter
from yuxi.workspace import filesystem
from yuxi.workspace.workdir import Workdir


@pytest.fixture
def artifact_scope(tmp_path, monkeypatch):
    """创建只属于本测试的真实 Workdir 和 adapter。"""
    root = tmp_path / "workspace"
    project = "projects/11111111-1111-4111-8111-111111111111"
    output_subdir = "pi-runs/0123456789abcdef01234567"
    output = root / project / "outputs" / output_subdir
    output.mkdir(parents=True)
    monkeypatch.setattr(filesystem, "user_workspace_dir", lambda _uid: root)
    adapter = LocalPiAdapter.__new__(LocalPiAdapter)
    adapter._workdir = Workdir.open_existing("user-1", project)
    adapter._output_subdir = output_subdir
    adapter._credentials = {}
    return adapter, output


def artifact_ref(output, files):
    """写入独立于被测摘要逻辑的已知 manifest。"""
    content = json.dumps({"files": files}).encode()
    (output / ".pi-artifacts.json").write_bytes(content)
    return {"path": ".pi-artifacts.json", "sha256": hashlib.sha256(content).hexdigest(), "files": files}


@pytest.mark.asyncio
async def test_artifact_streams_large_file_and_rejects_changed_bytes(artifact_scope, monkeypatch):
    adapter, output = artifact_scope
    block = b"x" * 1024 * 1024
    with (output / "large.bin").open("wb") as target:
        for _ in range(9):
            target.write(block)
    digest = hashlib.sha256()
    for _ in range(9):
        digest.update(block)
    files = [{"path": "large.bin", "size": 9 * len(block), "sha256": digest.hexdigest()}]
    ref = artifact_ref(output, files)
    monkeypatch.setattr(Workdir, "read_file", lambda *_: pytest.fail("must stream artifact bytes"))

    await adapter.validate_ref(ref)

    with (output / "large.bin").open("r+b") as target:
        target.write(b"y")
    with pytest.raises(ValueError, match="摘要不匹配"):
        await adapter.validate_ref(ref)


@pytest.mark.asyncio
async def test_artifact_rejects_credentials_crossing_chunk_boundary(artifact_scope):
    adapter, output = artifact_scope
    adapter._credentials = {"api_key": "secret-at-chunk-boundary"}
    content = b"x" * (1024 * 1024 - 5) + b"secret-at-chunk-boundary"
    (output / "secret.txt").write_bytes(content)
    ref = artifact_ref(
        output, [{"path": "secret.txt", "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}]
    )

    with pytest.raises(ValueError, match="模型凭据"):
        await adapter.validate_ref(ref)


@pytest.mark.asyncio
async def test_artifact_rejects_manifest_disagreement_and_duplicate_paths(artifact_scope):
    adapter, output = artifact_scope
    (output / "ok.txt").write_bytes(b"ok")
    file = {"path": "ok.txt", "size": 2, "sha256": hashlib.sha256(b"ok").hexdigest()}
    ref = artifact_ref(output, [file])
    ref["files"] = []
    with pytest.raises(ValueError, match="持久 manifest"):
        await adapter.validate_ref(ref)
    with pytest.raises(ValueError, match="重复路径"):
        await adapter.validate_ref(artifact_ref(output, [file, file]))


@pytest.mark.asyncio
async def test_artifact_enforces_file_count_file_size_and_total_budget(artifact_scope, monkeypatch):
    adapter, output = artifact_scope
    files = []
    for name in ("one.txt", "two.txt"):
        (output / name).write_bytes(b"ok")
        files.append({"path": name, "size": 2, "sha256": hashlib.sha256(b"ok").hexdigest()})
    ref = artifact_ref(output, files)
    with monkeypatch.context() as limits:
        limits.setattr(pi_execution_service, "PI_MAX_OUTPUT_FILES", 1)
        with pytest.raises(ValueError, match="文件清单无效"):
            await adapter.validate_ref(ref)
    with monkeypatch.context() as limits:
        limits.setattr(pi_execution_service, "PI_MAX_OUTPUT_FILE_BYTES", 1)
        with pytest.raises(ValueError, match="大小或摘要不匹配"):
            await adapter.validate_ref(ref)
    with monkeypatch.context() as limits:
        limits.setattr(pi_execution_service, "PI_MAX_OUTPUT_BYTES", 3)
        with pytest.raises(ValueError, match="总大小超过"):
            await adapter.validate_ref(ref)
    with (output / "oversized-ref.json").open("wb") as target:
        target.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="超过 16 MiB"):
        adapter.read_output("oversized-ref.json")


def test_stream_rejects_symlink_size_limit_and_changes_during_read(artifact_scope):
    adapter, output = artifact_scope
    (output / "file.txt").write_bytes(b"x" * (2 * 1024 * 1024))
    (output / "link.txt").symlink_to(output / "file.txt")
    (output / "linked").symlink_to(output, target_is_directory=True)
    for relative in ("link.txt", "linked/file.txt"):
        with pytest.raises(PermissionError, match="symlink"):
            list(adapter._workdir.iter_file_chunks(adapter._output_path(relative), 4 * 1024 * 1024))
    with pytest.raises(ValueError, match="transfer limit"):
        list(adapter._workdir.iter_file_chunks(adapter._output_path("file.txt"), 1))
    chunks = adapter._workdir.iter_file_chunks(adapter._output_path("file.txt"), 4 * 1024 * 1024)
    assert len(next(chunks)) == 1024 * 1024
    with (output / "file.txt").open("r+b") as target:
        target.write(b"changed")
        os.fsync(target.fileno())
    with pytest.raises(ValueError, match="changed while reading"):
        list(chunks)


@pytest.mark.parametrize("path", ["/absolute", "../escape", "a/../b", "a//b", "a/./b", "a\\b", "a\nb"])
def test_output_path_rejects_unsafe_names(artifact_scope, path):
    adapter, _ = artifact_scope
    with pytest.raises(ValueError, match="安全相对路径"):
        adapter.read_output(path)
