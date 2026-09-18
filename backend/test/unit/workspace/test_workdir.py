import os
from pathlib import Path

import pytest

from yuxi.workspace import filesystem as workspace_filesystem_module
from yuxi.workspace.workdir import Workdir


def test_open_existing_returns_workdir_capability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir_path = "projects/11111111-1111-4111-8111-111111111111"
    workspace_root = tmp_path / "workspace"
    (workspace_root / workdir_path).mkdir(parents=True)
    monkeypatch.setattr(workspace_filesystem_module, "user_workspace_dir", lambda _uid: workspace_root)

    workdir = Workdir.open_existing("user-1", workdir_path)

    assert workdir.relative_path == workdir_path
    assert workdir.root_path == f"/{workdir_path}"


def test_open_existing_rejects_missing_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setattr(workspace_filesystem_module, "user_workspace_dir", lambda _uid: workspace_root)

    with pytest.raises(FileNotFoundError):
        Workdir.open_existing("user-1", "projects/11111111-1111-4111-8111-111111111111")


def test_open_existing_rejects_regular_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir_path = "projects/11111111-1111-4111-8111-111111111111"
    workspace_root = tmp_path / "workspace"
    (workspace_root / "projects").mkdir(parents=True)
    (workspace_root / workdir_path).write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(workspace_filesystem_module, "user_workspace_dir", lambda _uid: workspace_root)

    with pytest.raises(ValueError, match="existing directory"):
        Workdir.open_existing("user-1", workdir_path)


def test_open_existing_rejects_symlinked_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir_path = "projects/11111111-1111-4111-8111-111111111111"
    workspace_root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace_root / "projects").mkdir(parents=True)
    (workspace_root / workdir_path).symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(workspace_filesystem_module, "user_workspace_dir", lambda _uid: workspace_root)

    with pytest.raises(PermissionError, match="symlink"):
        Workdir.open_existing("user-1", workdir_path)


def test_cleanup_preserves_selected_directory_and_removes_symlink_without_following_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workdir_path = "projects/11111111-1111-4111-8111-111111111111"
    workspace_root = tmp_path / "workspace"
    workdir_root = workspace_root / workdir_path
    outputs = workdir_root / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "result.txt").write_text("ok", encoding="utf-8")
    transient = workdir_root / "transient"
    transient.mkdir()
    (transient / "state.txt").write_text("remove", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (workdir_root / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(workspace_filesystem_module, "user_workspace_dir", lambda _uid: workspace_root)
    workdir = Workdir.open_existing("user-1", workdir_path)

    assert workdir.cleanup(preserve_directories=frozenset({"outputs"})) is False
    assert [item["name"] for item in workdir.list_directory()] == ["outputs"]
    assert workdir.read_file("/outputs/result.txt", 2) == b"ok"
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"

    assert workdir.cleanup() is True
    assert workdir_root.exists() is False


def test_stream_rejects_symlink_size_limit_and_changes_during_read(tmp_path, monkeypatch):
    """流式下载拒绝符号链接、超限与读取期间被修改的文件。"""
    project = "projects/11111111-1111-4111-8111-111111111111"
    root = tmp_path / "workspace"
    output = root / project / "outputs"
    output.mkdir(parents=True)
    monkeypatch.setattr(workspace_filesystem_module, "user_workspace_dir", lambda _uid: root)
    workdir = Workdir.open_existing("user-1", project)
    (output / "file.txt").write_bytes(b"x" * (2 * 1024 * 1024))
    (output / "link.txt").symlink_to(output / "file.txt")
    (output / "linked").symlink_to(output, target_is_directory=True)
    for relative in ("link.txt", "linked/file.txt"):
        with pytest.raises(PermissionError, match="symlink"):
            list(workdir.iter_file_chunks(f"/outputs/{relative}", 4 * 1024 * 1024))
    with pytest.raises(ValueError, match="transfer limit"):
        list(workdir.iter_file_chunks("/outputs/file.txt", 1))
    chunks = workdir.iter_file_chunks("/outputs/file.txt", 4 * 1024 * 1024)
    assert len(next(chunks)) == 1024 * 1024
    with (output / "file.txt").open("r+b") as target:
        target.write(b"changed")
        os.fsync(target.fileno())
    with pytest.raises(ValueError, match="changed while reading"):
        list(chunks)
