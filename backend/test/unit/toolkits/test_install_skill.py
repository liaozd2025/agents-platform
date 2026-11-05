"""退役 install_skill 工具的发布边界由个人 Skill 用例与远程草稿 Owner 验证。"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuxi.infrastructure.postgres.manager import pg_manager
from yuxi.modules.extensions.skills import install_draft, personal, remote
from yuxi.modules.identity.models import User
from yuxi.modules.workspace import paths
from yuxi.modules.workspace.paths import user_workspace_dir


@pytest.fixture
def source(tmp_path, monkeypatch):
    """使用本例独立持久源，不触碰 Agent 配置或真实沙盒。"""
    monkeypatch.setattr(paths, "get_user_data_dir", lambda: tmp_path / "users")
    monkeypatch.setenv("YUXI_SKILL_DATA_DIR", str(tmp_path / "skills"))
    directory = tmp_path / "source"
    directory.mkdir()
    (directory / "SKILL.md").write_text("---\nname: demo\ndescription: personal skill\n---\nOriginal body", encoding="utf-8")
    return directory


@pytest.mark.asyncio
async def test_personal_install_is_private_without_database_and_offloads_io(source, monkeypatch):
    """安装不读 Agent 配置，真实文件复制在线程池中执行且结果属于当前用户。"""

    def no_database():
        raise AssertionError("个人安装不得读取 Agent 数据库配置")

    monkeypatch.setattr(pg_manager, "get_async_session_context", no_database)
    main_thread = threading.get_ident()
    original_copy = personal.copy_skill_snapshot
    threads = []

    def copy(*args, **kwargs):
        threads.append(threading.get_ident())
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(personal, "copy_skill_snapshot", copy)
    result = await personal.install_personal_skill_dir("current-user", source, origin="remote")
    installed = user_workspace_dir("current-user") / "agents/skills/demo/SKILL.md"
    assert installed.read_bytes() == (source / "SKILL.md").read_bytes()
    assert result.created_by == "current-user" and result.source_scope == "personal"
    assert result.origin == "remote" and result.enabled is True
    assert threads and all(thread != main_thread for thread in threads)
    assert result.tool_dependencies == result.mcp_dependencies == result.skill_dependencies == []
    assert not (user_workspace_dir("other-user") / "agents/skills/demo").exists()


@pytest.mark.asyncio
async def test_duplicate_install_preserves_existing_content_and_state(source):
    """重复安装不得覆盖已有内容或把用户禁用状态重置为启用。"""
    await personal.install_personal_skill_dir("user", source, origin="remote")
    await personal.update_personal_skill_enabled("user", "demo", enabled=False)
    original = (user_workspace_dir("user") / "agents/skills/demo/SKILL.md").read_bytes()
    (source / "SKILL.md").write_text("---\nname: demo\ndescription: replacement\n---\nReplace", encoding="utf-8")
    with pytest.raises(ValueError, match="已存在同名 Skill"):
        await personal.install_personal_skill_dir("user", source)
    assert (user_workspace_dir("user") / "agents/skills/demo/SKILL.md").read_bytes() == original
    item = (await personal.list_personal_skills("user"))[0]
    assert item.enabled is False and item.origin == "remote"
    assert not list((user_workspace_dir("user") / "agents/skills").glob(".install.tmp-*"))


@pytest.mark.asyncio
async def test_failed_publish_leaves_no_half_installed_skill(source, monkeypatch):
    """原子发布失败保留原异常，不把暂存内容当作已安装成果。"""

    def fail(*args):
        raise OSError("fixture publish failed")

    monkeypatch.setattr(personal, "publish_directory_fd", fail)
    with pytest.raises(OSError, match="fixture publish failed"):
        await personal.install_personal_skill_dir("user", source)
    assert await personal.list_personal_skills("user") == []
    assert not list((user_workspace_dir("user") / "agents/skills").glob(".install.tmp-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("source_text,skills", [("", ["demo"]), ("owner/repo", [])])
async def test_remote_source_and_selection_are_required_before_sandbox_access(monkeypatch, source_text, skills):
    """来源和选择不完整时，不访问沙盒或把路径交给宿主文件系统。"""
    monkeypatch.setattr(
        remote, "remote_skill_source_policy", SimpleNamespace(get=AsyncMock(return_value={"allowed_hosts": ["github.com"]}))
    )

    def no_sandbox(*args):
        raise AssertionError("非法远程请求不得创建沙盒")

    monkeypatch.setattr(remote._RemoteSkillRequest, "create", no_sandbox)
    with pytest.raises(ValueError):
        await remote.download_remote_skills(source=source_text, skills=skills, uid="user")


@pytest.mark.asyncio
async def test_remote_draft_installs_for_authenticated_owner_and_cleans_download(source, tmp_path, monkeypatch):
    """远程准备只产出草稿；确认发布保留来源并清理已消费临时内容。"""
    downloaded = tmp_path / "download"
    downloaded.mkdir()
    package = downloaded / "demo"
    package.mkdir()
    (package / "SKILL.md").write_bytes((source / "SKILL.md").read_bytes())

    async def prepare(*, source, skills, uid):
        assert source == "owner/repo" and skills == ["demo"] and uid == "user"
        return remote.RemoteSkillDownloads(temp_home=str(downloaded), results=[remote.DownloadedSkill(slug="demo", source_dir=package)])

    monkeypatch.setattr(remote, "download_remote_skills", prepare)
    user = User(uid="user")
    draft = await install_draft.create_remote_skill_draft(source="owner/repo", skills=["demo"], operator=user)
    assert not downloaded.exists()
    assert await personal.list_personal_skills("user") == []
    result = await personal.confirm_personal_skill_install_draft(draft_id=draft["draft_id"], slugs=["demo"], operator=user)
    assert result[0]["success"] is True and result[0]["skill"]["origin"] == "remote"
    assert (user_workspace_dir("user") / "agents/skills/demo/SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
    with pytest.raises(ValueError):
        await personal.confirm_personal_skill_install_draft(draft_id=draft["draft_id"], slugs=["demo"], operator=user)


@pytest.mark.asyncio
async def test_draft_cannot_be_confirmed_by_another_user(source):
    """草稿归属不能被客户端指定的另一名用户绕过。"""
    draft = await install_draft.create_uploaded_skill_draft(
        filename="SKILL.md", file_bytes=(source / "SKILL.md").read_bytes(), operator=User(uid="owner")
    )
    with pytest.raises(ValueError, match="无权"):
        await personal.confirm_personal_skill_install_draft(draft_id=draft["draft_id"], slugs=["demo"], operator=User(uid="other"))
    assert await personal.list_personal_skills("other") == []
