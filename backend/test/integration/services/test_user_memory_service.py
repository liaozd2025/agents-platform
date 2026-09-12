"""真实 PostgreSQL 资料通过上下文构建写入隔离工作区。"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from yuxi.services import user_memory_service

from yuxi.agents.context import build_agent_input_context
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Department, Role, User, UserConfig, UserRoleAssignment
from yuxi.workspace.filesystem import Workspace
from yuxi.workspace.paths import ensure_user_workspace


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """本模块不创建沙盒，不清理其他测试资源。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """本模块仅使用回滚事务与 tmp_path。"""
    yield


@pytest_asyncio.fixture(autouse=True)
async def close_profile_database():
    """关闭本测试进程的连接池，避免事件循环结束后遗留连接。"""
    yield
    await pg_manager.close()


@pytest.mark.asyncio
async def test_profile_sync_uses_current_roles_and_preserves_manual_content(tmp_path, monkeypatch):
    """真实角色变更、停用和 Memory 关闭均影响下一次模型输入。"""
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path))
    pg_manager.initialize()
    uid = "pytest-memory-" + uuid.uuid4().hex
    async with pg_manager.get_async_session_context() as db:
        department = Department(name=uid, path="/")
        role = Role(code=uid, name="资料测试角色", default_scope_type="self", is_active=True)
        db.add_all([department, role])
        await db.flush()
        user = User(username=uid, uid=uid, password_hash="unused-test-hash", department_id=department.id)
        config = UserConfig(uid=uid, enable_memory=True)
        db.add_all([user, config])
        await db.flush()
        db.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_mode="inherit"))
        await db.flush()
        ensure_user_workspace(uid)
        workspace = Workspace(uid)
        workspace.replace_authorized_file("/agents/USER.md", "手工偏好：中文\n".encode())

        async def context():
            """调用真实上下文入口并回读文件与模型输入。"""
            result = await build_agent_input_context({}, thread_id="memory-test", uid=uid, db=db)
            content = workspace.read_authorized_file("/agents/USER.md", 1024 * 1024).decode()
            assert content.strip() in result["system_prompt"]
            assert "手工偏好：中文" in content
            return content

        content = await context()
        assert f"- 用户名：{uid}" in content
        assert f"- 部门：{uid}" in content
        assert "- 角色：资料测试角色" in content
        assert "unused-test-hash" not in content
        department.name = "更新部门"
        role.name = "更新角色"
        await db.flush()
        updated = await context()
        assert "更新部门" in updated and "更新角色" in updated
        assert "资料测试角色" not in updated
        role.is_active = False
        await db.flush()
        assert "- 角色：未分配" in await context()
        config.enable_memory = False
        await db.flush()
        assert await context() == "手工偏好：中文\n"

        async def fail_query(**kwargs):
            """制造 SQL 错误，验证可选同步不污染外层事务。"""
            await kwargs["db"].execute(text("SELECT 1 / 0"))

        monkeypatch.setattr(user_memory_service, "sync_user_profile_to_memory", fail_query)
        assert await context() == "手工偏好：中文\n"
        assert await db.scalar(select(1)) == 1
        await db.rollback()
