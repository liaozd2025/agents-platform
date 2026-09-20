"""真实 PostgreSQL 资料通过上下文构建写入隔离工作区。"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from yuxi.services import user_memory_service

from yuxi.agents.context import build_agent_input_context
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import ROOT_DEPARTMENT_ID, Department, User, UserConfig
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
async def test_profile_sync_uses_current_profile_and_preserves_manual_content(tmp_path, monkeypatch):
    """真实资料变更与 Memory 关闭均影响下一次模型输入，角色不进入用户资料。"""
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path))
    pg_manager.initialize()
    uid = "pytest-memory-" + uuid.uuid4().hex
    async with pg_manager.get_async_session_context() as db:
        department = Department(name=uid, path="/")
        db.add(department)
        await db.flush()
        # 补上真实物化路径，用于验证「集团 > 部门」组织链路（不是单写部门名）
        department.path = f"/{ROOT_DEPARTMENT_ID}/{department.id}/"
        await db.flush()
        root = await db.get(Department, ROOT_DEPARTMENT_ID)
        assert root is not None, "资料同步测试需要现有集团根节点"
        expected_department = f"{root.name} > {uid}"
        user = User(
            username=uid,
            display_name="资料测试姓名",
            uid=uid,
            password_hash="unused-test-hash",
            department_id=department.id,
            oa_station_name="资料测试岗位",
            oa_job_level_name="资料测试职级",
        )
        config = UserConfig(uid=uid, enable_memory=True)
        db.add_all([user, config])
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
        # 真实资料同步必须写展示姓名、岗位与职级，而不是登录账号
        assert "- 称呼：资料测试姓名" in content
        assert f"- 称呼：{uid}" not in content
        # 部门写组织链路，不是只写直属部门名
        assert f"- 部门：{expected_department}" in content
        assert "- 岗位：资料测试岗位" in content
        assert "- 职级：资料测试职级" in content
        # 角色进入模型上下文会造成误导，用户资料里不得出现；UID 对模型没有决策价值
        assert "- 角色" not in content
        assert "- UID" not in content
        assert "unused-test-hash" not in content
        # 展示姓名缺失时回退登录账号
        user.display_name = None
        await db.flush()
        assert f"- 称呼：{uid}" in await context()
        user.display_name = "资料测试姓名"
        await db.flush()
        department.name = "更新部门"
        user.oa_station_name = "更新岗位"
        user.oa_job_level_name = "更新职级"
        await db.flush()
        updated = await context()
        assert "更新部门" in updated and "更新岗位" in updated and "更新职级" in updated
        assert "资料测试岗位" not in updated and "资料测试职级" not in updated
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
