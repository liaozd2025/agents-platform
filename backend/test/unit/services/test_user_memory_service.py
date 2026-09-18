from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxi.workspace import paths as workspace_paths
from yuxi.services import user_memory_service as svc


class _Result:
    def __init__(self, row):
        self._row = row

    def all(self):
        """同时服务资料投影与组织链路两次查询：后者取到同一行也不会命中节点 ID，链路退回部门名。"""
        # 列序与被测查询保持一致：
        # 0=username、1=display_name、2=部门名、3=Memory 开关、4=OA 岗位、5=OA 职级、6=部门物化路径
        user, department, enabled = self._row
        return [
            (
                user.username,
                user.display_name,
                department,
                enabled,
                user.oa_station_name,
                user.oa_job_level_name,
                "",
            )
        ]


class _DB:
    def __init__(self, row):
        self.row = row

    async def execute(self, _query):
        return _Result(self.row)


def _user(
    display_name: str | None = "张三",
    station_name: str | None = None,
    job_level_name: str | None = None,
) -> SimpleNamespace:
    """登录账号与展示名刻意不同：确保断言验证的是 display_name 而不是 username。"""
    return SimpleNamespace(
        username="2024102811",
        display_name=display_name,
        oa_station_name=station_name,
        oa_job_level_name=job_level_name,
    )


@pytest.mark.asyncio
async def test_sync_profile_writes_database_user_and_department_and_keeps_manual_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path / "threads"))
    workspace_paths.ensure_user_workspace("user-1")
    memory_file = tmp_path / "threads" / "shared" / "user-1" / "workspace" / "agents" / "USER.md"
    # 刻意用历史文件头，验证同步时会升级为「关于我」且保留区块外手工内容
    memory_file.write_text(f"{svc.LEGACY_PREAMBLE}\n手工偏好：使用中文\n", encoding="utf-8")

    await svc.sync_user_profile_to_memory(
        db=_DB((_user(station_name="中级前端程序员", job_level_name="11（基层）"), "研发部", True)),
        uid="user-1",
    )

    content = memory_file.read_text(encoding="utf-8")
    assert content.startswith("# 关于我")
    assert "# USER" not in content and "以下是有关用户的一些信息" not in content
    assert "手工偏好：使用中文" in content
    assert "- 称呼：张三" in content
    # 关键回归点：展示名存在时不能退回登录账号（修复前这里会写成 2024102811）
    assert "- 称呼：2024102811" not in content
    assert "- 部门：研发部" in content
    assert "- 岗位：中级前端程序员" in content
    assert "- 职级：11（基层）" in content
    # 角色与 UID 都不进用户画像：前者属权限信息，后者对模型没有决策价值
    assert "- 角色" not in content
    assert "- UID" not in content
    assert "password" not in content

    await svc.sync_user_profile_to_memory(
        db=_DB((_user(station_name="中级前端程序员", job_level_name="11（基层）"), "研发部", True)),
        uid="user-1",
    )
    assert memory_file.read_text(encoding="utf-8") == content


@pytest.mark.asyncio
async def test_sync_profile_omits_station_line_without_oa_station(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未反查到岗位与职级（本地账号或 OA 未返回）时不产生空行。"""
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path / "threads"))
    workspace_paths.ensure_user_workspace("user-1")
    memory_file = tmp_path / "threads" / "shared" / "user-1" / "workspace" / "agents" / "USER.md"
    memory_file.write_text("# 关于我\n", encoding="utf-8")

    await svc.sync_user_profile_to_memory(
        db=_DB((_user(), "研发部", True)),
        uid="user-1",
    )

    content = memory_file.read_text(encoding="utf-8")
    assert content.startswith("# 关于我")
    assert "- 部门：研发部" in content
    assert "- 岗位：" not in content
    assert "- 职级：" not in content
    # 岗位与职级行缺失时区块的其余内容仍完整；UID 不作为用户画像写入
    assert "- UID" not in content
    assert svc.PROFILE_END in content


@pytest.mark.asyncio
async def test_sync_profile_falls_back_to_login_account_without_display_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未维护展示名时回退登录账号，保证「称呼」一行不为空。"""
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path / "threads"))
    workspace_paths.ensure_user_workspace("user-1")
    memory_file = tmp_path / "threads" / "shared" / "user-1" / "workspace" / "agents" / "USER.md"
    memory_file.write_text("# 关于我\n", encoding="utf-8")

    await svc.sync_user_profile_to_memory(
        db=_DB((_user(display_name=None), "研发部", True)),
        uid="user-1",
    )

    assert "- 称呼：2024102811" in memory_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_sync_profile_removes_managed_block_when_memory_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path / "threads"))
    workspace_paths.ensure_user_workspace("user-1")
    memory_file = tmp_path / "threads" / "shared" / "user-1" / "workspace" / "agents" / "USER.md"
    memory_file.write_text(
        f"# 关于我\n\n手工内容\n\n{svc.PROFILE_START}\n- 称呼：旧名字\n{svc.PROFILE_END}\n",
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
