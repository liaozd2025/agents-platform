from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxi.workspace import paths as workspace_paths
from yuxi.services import user_memory_service as svc


class _Result:
    def __init__(self, row):
        self._row = row

    def all(self):
        user, department, enabled = self._row
        return [
            (user.username, department, enabled, a.role.name if a.role.is_active else None)
            for a in user.role_assignments
        ]


class _DB:
    def __init__(self, row):
        self.row = row

    async def execute(self, _query):
        return _Result(self.row)


def _user() -> SimpleNamespace:
    return SimpleNamespace(
        username="张三",
        uid="user-1",
        role_assignments=[
            SimpleNamespace(role=SimpleNamespace(name="普通用户", is_active=True)),
            SimpleNamespace(role=SimpleNamespace(name="停用角色", is_active=False)),
        ],
    )


@pytest.mark.asyncio
async def test_sync_profile_writes_database_user_and_department_and_keeps_manual_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path / "threads"))
    workspace_paths.ensure_user_workspace("user-1")
    memory_file = tmp_path / "threads" / "shared" / "user-1" / "workspace" / "agents" / "USER.md"
    memory_file.write_text("# USER\n\n手工偏好：使用中文\n", encoding="utf-8")

    await svc.sync_user_profile_to_memory(
        db=_DB((_user(), "研发部", True)),
        uid="user-1",
    )

    content = memory_file.read_text(encoding="utf-8")
    assert "手工偏好：使用中文" in content
    assert "- 用户名：张三" in content
    assert "- 部门：研发部" in content
    assert "- 角色：普通用户" in content
    assert "停用角色" not in content
    assert "password" not in content

    await svc.sync_user_profile_to_memory(
        db=_DB((_user(), "研发部", True)),
        uid="user-1",
    )
    assert memory_file.read_text(encoding="utf-8") == content


@pytest.mark.asyncio
async def test_sync_profile_removes_managed_block_when_memory_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path / "threads"))
    workspace_paths.ensure_user_workspace("user-1")
    memory_file = tmp_path / "threads" / "shared" / "user-1" / "workspace" / "agents" / "USER.md"
    memory_file.write_text(
        f"# USER\n\n手工内容\n\n{svc.PROFILE_START}\n## 用户资料\n- 用户名：旧名字\n{svc.PROFILE_END}\n",
        encoding="utf-8",
    )

    await svc.sync_user_profile_to_memory(
        db=_DB((_user(), "研发部", False)),
        uid="user-1",
    )

    content = memory_file.read_text(encoding="utf-8")
    assert "手工内容" in content
    assert svc.PROFILE_START not in content
    assert "旧名字" not in content


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["file_symlink", "directory_symlink", "oversized"])
async def test_sync_profile_preserves_unsafe_or_oversized_file(tmp_path, monkeypatch, unsafe):
    """不安全路径和超大手工文件不能被同步覆盖。"""
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path / "threads"))
    workspace_paths.ensure_user_workspace("user-1")
    agents = workspace_paths.user_workspace_dir("user-1") / "agents"
    memory_file = agents / "USER.md"
    outside = tmp_path / "outside"
    outside.mkdir()
    original = b"manual" if unsafe != "oversized" else b"x" * (1024 * 1024 + 1)
    target = outside / "USER.md" if unsafe != "oversized" else memory_file
    target.write_bytes(original)
    if unsafe == "file_symlink":
        memory_file.unlink()
        memory_file.symlink_to(target)
    elif unsafe == "directory_symlink":
        agents.rename(agents.with_name("original-agents"))
        agents.symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        await svc.sync_user_profile_to_memory(db=_DB((_user(), "研发部", True)), uid="user-1")
    assert target.read_bytes() == original
