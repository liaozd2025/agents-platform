from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxi.agents.backends.sandbox import paths as workspace_paths
from yuxi.services import user_memory_service as svc


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _DB:
    def __init__(self, row):
        self.row = row

    async def execute(self, _query):
        return _Result(self.row)


def _user() -> SimpleNamespace:
    return SimpleNamespace(username="张三", uid="user-1", role="user")


@pytest.mark.asyncio
async def test_sync_profile_writes_database_user_and_department_and_keeps_manual_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_paths.conf, "save_dir", str(tmp_path))
    workspace_paths.ensure_thread_dirs("thread-1", "user-1")
    memory_file = tmp_path / "threads" / "shared" / "user-1" / "workspace" / "agents" / "USER.md"
    memory_file.write_text("# USER\n\n手工偏好：使用中文\n", encoding="utf-8")

    await svc.sync_user_profile_to_memory(
        db=_DB((_user(), "研发部", True)),
        uid="user-1",
        thread_id="thread-1",
    )

    content = memory_file.read_text(encoding="utf-8")
    assert "手工偏好：使用中文" in content
    assert "- 用户名：张三" in content
    assert "- 部门：研发部" in content
    assert "password" not in content

    await svc.sync_user_profile_to_memory(
        db=_DB((_user(), "研发部", True)),
        uid="user-1",
        thread_id="thread-1",
    )
    assert memory_file.read_text(encoding="utf-8") == content


@pytest.mark.asyncio
async def test_sync_profile_removes_managed_block_when_memory_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_paths.conf, "save_dir", str(tmp_path))
    workspace_paths.ensure_thread_dirs("thread-1", "user-1")
    memory_file = tmp_path / "threads" / "shared" / "user-1" / "workspace" / "agents" / "USER.md"
    memory_file.write_text(
        "# USER\n\n手工内容\n\n"
        f"{svc.PROFILE_START}\n## 用户资料\n- 用户名：旧名字\n{svc.PROFILE_END}\n",
        encoding="utf-8",
    )

    await svc.sync_user_profile_to_memory(
        db=_DB((_user(), "研发部", False)),
        uid="user-1",
        thread_id="thread-1",
    )

    content = memory_file.read_text(encoding="utf-8")
    assert "手工内容" in content
    assert svc.PROFILE_START not in content
    assert "旧名字" not in content
