"""真实 PostgreSQL 资料通过当前 Context 准备写入隔离工作区。"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from test.integration.support.module_audit import isolated_audit_store
from yuxi.modules.agents.runtime.context import BaseContext, prepare_agent_runtime_context
from yuxi.modules.agents.services import user_memory
from yuxi.modules.identity.models import Department, User, UserConfig
from yuxi.modules.workspace.filesystem import Workspace
from yuxi.modules.workspace.paths import ensure_user_workspace

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


async def test_profile_sync_uses_current_profile_and_preserves_manual_content(tmp_path, monkeypatch):
    """资料变更和 Memory 关闭进入下一次模型上下文，密码与角色不进入画像。"""
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path))
    # 本例不调用模型，仅给上下文提供一个可解析的模型目录项。
    monkeypatch.setattr("yuxi.modules.models.providers.cache.model_cache.get_model_info", lambda _: SimpleNamespace(model_type="chat"))
    uid = "pytest-memory-" + uuid.uuid4().hex
    async with isolated_audit_store(monkeypatch) as (_manager, sessions):
        async with sessions() as db:
            root = Department(name="测试集团", node_type="group")
            db.add(root)
            await db.flush()
            root.path = f"/{root.id}/"
            department = Department(name=uid, parent_id=root.id)
            db.add(department)
            await db.flush()
            department.path = f"{root.path}{department.id}/"
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
            await db.commit()
            ensure_user_workspace(uid)
            workspace = Workspace(uid)
            workspace.replace_authorized_file("/agents/USER.md", "手工偏好：中文\n".encode())

            async def context():
                """每次构造新 Context，经实际准备流程回读已提交资料与画像文件。"""
                result = await prepare_agent_runtime_context(
                    BaseContext(
                        uid=uid,
                        thread_id="memory-test",
                        model="profile-only",
                        tools=[],
                        knowledges=[],
                        skills=[],
                        preload_skills=[],
                        mcps=[],
                    )
                )
                content = workspace.read_authorized_file("/agents/USER.md", 1024 * 1024).decode()
                assert content.strip() in result.system_prompt
                assert result._runtime_prepared is True
                assert "手工偏好：中文" in content
                return content

            content = await context()
            assert "- 称呼：资料测试姓名" in content
            assert f"- 称呼：{uid}" not in content
            assert f"- 部门：测试集团 > {uid}" in content
            assert "- 岗位：资料测试岗位" in content
            assert "- 职级：资料测试职级" in content
            assert "- 角色" not in content and "- UID" not in content
            assert "unused-test-hash" not in content
            user.display_name = None
            await db.commit()
            assert f"- 称呼：{uid}" in await context()
            user.display_name = "资料测试姓名"
            department.name = "更新部门"
            user.oa_station_name = "更新岗位"
            user.oa_job_level_name = "更新职级"
            await db.commit()
            updated = await context()
            assert "更新部门" in updated and "更新岗位" in updated and "更新职级" in updated
            assert "资料测试岗位" not in updated and "资料测试职级" not in updated
            config.enable_memory = False
            await db.commit()
            assert await context() == "手工偏好：中文\n"

            async def fail_query(**kwargs):
                """同步SQL失败应回滚自己的savepoint，后续真实授权读取仍成功。"""
                await kwargs["db"].execute(text("SELECT 1 / 0"))

            monkeypatch.setattr(user_memory, "sync_user_profile_to_memory", fail_query)
            assert await context() == "手工偏好：中文\n"
