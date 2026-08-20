"""组织节点数据访问层 - Repository"""

from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import (
    DEPARTMENT_NODE_TYPE,
    GROUP_NODE_TYPE,
    ROOT_DEPARTMENT_ID,
    APIKey,
    Department,
    User,
)


def build_child_path(parent_path: str, node_id: int) -> str:
    """由父节点的物化路径与自身 ID 拼出子节点路径，形如 /1/3/7/"""
    return f"{parent_path}{node_id}/"


class DepartmentRepository:
    """组织节点数据访问层"""

    def __init__(self, db_session: AsyncSession | None = None):
        self.db_session = db_session

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        """复用请求会话，未注入时创建独立事务会话。"""
        if self.db_session is not None:
            yield self.db_session
            return
        async with pg_manager.get_async_session_context() as session:
            yield session

    async def get_by_id(self, id: int) -> Department | None:
        """根据 ID 获取部门"""
        async with self._session() as session:
            result = await session.execute(select(Department).where(Department.id == id))
            return result.scalar_one_or_none()

    async def get_name_by_id(self, id: int) -> str | None:
        """根据 ID 获取部门名称。"""
        async with self._session() as session:
            result = await session.execute(select(Department.name).where(Department.id == id))
            return result.scalar_one_or_none()

    async def get_with_user_count(self, id: int) -> dict[str, Any] | None:
        """获取部门及其未删除用户数量。"""
        async with self._session() as session:
            result = await session.execute(select(Department).where(Department.id == id))
            department = result.scalar_one_or_none()
            if department is None:
                return None
            count_result = await session.execute(
                select(func.count(User.id)).where(User.department_id == id, User.is_deleted == 0)
            )
            return {**department.to_dict(), "user_count": count_result.scalar() or 0}

    async def get_by_name(self, name: str) -> Department | None:
        """根据名称获取部门"""
        async with self._session() as session:
            result = await session.execute(select(Department).where(Department.name == name))
            return result.scalar_one_or_none()

    async def list_departments(self) -> list[Department]:
        """获取所有组织节点，按物化路径排序，父节点必定排在其子节点之前"""
        async with self._session() as session:
            result = await session.execute(select(Department).order_by(Department.path))
            return list(result.scalars().all())

    async def get_paths_by_ids(
        self,
        ids: Collection[int],
        *,
        session: AsyncSession | None = None,
    ) -> dict[int, str]:
        """一次读取保存权限配置所需的组织节点路径。"""

        if not ids:
            return {}

        statement = select(Department.id, Department.path).where(Department.id.in_(ids))
        if session is not None:
            result = await session.execute(statement)
            return dict(result.all())

        async with self._session() as managed_session:
            result = await managed_session.execute(statement)
            return dict(result.all())

    async def get_names_by_ids(
        self,
        ids: Collection[int],
        *,
        session: AsyncSession | None = None,
    ) -> dict[int, str]:
        """一次读取指定组织节点名称，并保持物化路径顺序。"""

        if not ids:
            return {}

        statement = select(Department.id, Department.name).where(Department.id.in_(ids)).order_by(Department.path)
        if session is not None:
            result = await session.execute(statement)
            return dict(result.all())

        async with self._session() as managed_session:
            result = await managed_session.execute(statement)
            return dict(result.all())

    async def list_with_user_count(self, *, session: AsyncSession | None = None) -> list[dict[str, Any]]:
        """获取所有组织节点，按物化路径排序并附带用户数量"""
        statement = (
            select(Department, func.count(User.id))
            .outerjoin(User, (User.department_id == Department.id) & (User.is_deleted == 0))
            .group_by(Department.id)
            .order_by(Department.path)
        )
        if session is not None:
            result = await session.execute(statement)
            return [{**department.to_dict(), "user_count": user_count} for department, user_count in result.all()]

        async with self._session() as managed_session:
            result = await managed_session.execute(statement)
            return [{**department.to_dict(), "user_count": user_count} for department, user_count in result.all()]

    async def create_child(
        self,
        session: AsyncSession,
        *,
        name: str,
        description: str | None,
        parent: Department,
        node_type: str = DEPARTMENT_NODE_TYPE,
    ) -> Department:
        """在指定父节点下创建组织节点，落库后回填物化路径"""
        department = Department(
            name=name,
            description=description,
            parent_id=parent.id,
            node_type=node_type,
        )
        session.add(department)
        # 路径依赖自增主键，须先 flush 拿到 ID 再拼接
        await session.flush()
        department.path = build_child_path(parent.path, department.id)
        return department

    async def create_group_root(self, *, name: str, description: str | None) -> Department:
        """创建集团根节点，仅用于系统初始化"""
        async with self._session() as session:
            department = Department(name=name, description=description, parent_id=None, node_type=GROUP_NODE_TYPE)
            session.add(department)
            await session.flush()
            # 迁移语句与回落逻辑都按 ROOT_DEPARTMENT_ID 定位集团根，拿到别的 ID 说明库不是干净的
            if department.id != ROOT_DEPARTMENT_ID:
                raise RuntimeError(f"集团根必须占用 id={ROOT_DEPARTMENT_ID}，实际得到 {department.id}")
            department.path = build_child_path("/", department.id)
        return department

    async def update(self, id: int, data: dict[str, Any]) -> Department | None:
        """更新部门"""
        async with self._session() as session:
            result = await session.execute(select(Department).where(Department.id == id))
            department = result.scalar_one_or_none()
            if department is None:
                return None
            for key, value in data.items():
                if key != "id":
                    setattr(department, key, value)
            await session.flush()
            await session.refresh(department)
        return department

    async def save(self, department: Department, *, refresh: bool = False) -> Department:
        """保存当前事务中的组织节点修改。"""

        async with self._session() as session:
            await session.flush()
            if refresh:
                await session.refresh(department)
        return department

    async def delete(self, id: int) -> bool:
        """删除部门"""
        async with self._session() as session:
            result = await session.execute(select(Department).where(Department.id == id))
            department = result.scalar_one_or_none()
            if department is None:
                return False
            await session.delete(department)
            await session.flush()
        return True

    async def count_users(self, id: int) -> int:
        """统计部门用户数量"""
        async with self._session() as session:
            result = await session.execute(
                select(func.count(User.id)).where(User.department_id == id, User.is_deleted == 0)
            )
            return result.scalar() or 0

    async def count_children(self, id: int) -> int:
        """统计组织节点的直属子节点数量。"""

        async with self._session() as session:
            result = await session.execute(select(func.count(Department.id)).where(Department.parent_id == id))
            return result.scalar() or 0

    async def delete_empty_node(self, department: Department) -> None:
        """迁移软删除用户并移除空组织节点及其 API Key。"""

        async with self._session() as session:
            await session.execute(
                update(User).where(User.department_id == department.id).values(department_id=ROOT_DEPARTMENT_ID)
            )
            await session.execute(delete(APIKey).where(APIKey.department_id == department.id))
            await session.delete(department)
            await session.flush()

    async def exists_sibling_name(self, parent_id: int | None, name: str) -> bool:
        """检查同一父节点下是否已有同名组织节点；跨父节点重名是允许的"""
        async with self._session() as session:
            result = await session.execute(
                select(Department.id).where(Department.parent_id == parent_id, Department.name == name)
            )
            return result.first() is not None

    async def move_subtree(
        self,
        session: AsyncSession,
        department: Department,
        target_parent: Department,
    ) -> None:
        """在当前事务内更新父节点，并批量替换整棵子树的物化路径前缀。"""
        old_path = department.path
        new_path = build_child_path(target_parent.path, department.id)
        await session.execute(
            update(Department)
            .where(Department.path.like(f"{old_path}%"))
            .values(path=func.concat(new_path, func.substr(Department.path, len(old_path) + 1)))
        )
        department.parent_id = target_parent.id

    async def exists_by_name(self, name: str) -> bool:
        """检查部门名称是否存在"""
        async with self._session() as session:
            result = await session.execute(select(Department.id).where(Department.name == name))
            return result.scalar_one_or_none() is not None
