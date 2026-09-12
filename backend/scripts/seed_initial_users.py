from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TypedDict

from dotenv import load_dotenv
from sqlalchemy import func, select


APP_ROOT = Path(__file__).resolve().parents[1]
for import_path in (APP_ROOT, APP_ROOT / "package"):
    import_path_str = str(import_path)
    if import_path_str not in sys.path:
        sys.path.insert(0, import_path_str)

SUPERADMIN_UID = "zwj"
SUPERADMIN_NAME = "张文杰"
SUPERADMIN_PHONE_NUMBER = "15251638888"
SUPERADMIN_PASSWORD = "zwj12138"
DEFAULT_USER_PASSWORD = "yuxi123456"


class DepartmentSeed(TypedDict):
    name: str
    description: str
    prefix: str
    normal_count: int


GROUP_ROOT_NAME = "示例集团"
GROUP_ROOT_DESCRIPTION = "种子数据的集团根节点"

DEPARTMENTS: list[DepartmentSeed] = [
    {"name": "研发部", "description": "负责产品研发与技术平台建设", "prefix": "dev", "normal_count": 5},
    {"name": "产品部", "description": "负责产品规划、需求分析与项目推进", "prefix": "prod", "normal_count": 5},
    {"name": "运营部", "description": "负责业务运营、用户支持与内容维护", "prefix": "ops", "normal_count": 4},
]


class SeedError(Exception):
    pass


def load_project_env() -> None:
    load_dotenv(APP_ROOT / ".env", override=False)
    load_dotenv(APP_ROOT.parent / ".env", override=False)
    load_dotenv(Path.cwd() / ".env", override=False)


async def ensure_uninitialized(session) -> None:
    from yuxi.storage.postgres.models_business import User

    user_count = await session.scalar(select(func.count(User.id)))
    if user_count:
        raise SeedError(f"系统已初始化：users 表已有 {user_count} 个用户，脚本已退出。")


async def seed_initial_users() -> None:
    from yuxi.utils.auth_utils import AuthUtils
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.repositories.department_repository import build_child_path
    from yuxi.repositories.user_repository import UserRepository
    from yuxi.storage.postgres.models_business import GROUP_NODE_TYPE, Department
    from yuxi.utils.datetime_utils import utc_now_naive

    try:
        pg_manager.initialize()
        await pg_manager.create_business_tables()
        await pg_manager.ensure_business_schema()

        async with pg_manager.get_async_session_context() as session:
            await ensure_uninitialized(session)

            # 集团根必须先落库：它要占住固定的 id=1，其余节点都挂在它下面
            group_root = Department(
                name=GROUP_ROOT_NAME,
                description=GROUP_ROOT_DESCRIPTION,
                parent_id=None,
                node_type=GROUP_NODE_TYPE,
            )
            session.add(group_root)
            await session.flush()
            group_root.path = build_child_path("/", group_root.id)

            departments: dict[str, Department] = {}
            for department_seed in DEPARTMENTS:
                department = Department(
                    name=department_seed["name"],
                    description=department_seed["description"],
                    parent_id=group_root.id,
                )
                session.add(department)
                departments[department_seed["prefix"]] = department

            await session.flush()

            for department in departments.values():
                department.path = build_child_path(group_root.path, department.id)

            user_repo = UserRepository()
            await user_repo.create_with_db(
                session,
                {
                    "username": SUPERADMIN_NAME,
                    "uid": SUPERADMIN_UID,
                    "phone_number": SUPERADMIN_PHONE_NUMBER,
                    "password_hash": AuthUtils.hash_password(SUPERADMIN_PASSWORD),
                    "department_id": departments["dev"].id,
                    "last_login": utc_now_naive(),
                },
                default_role_code="superadmin",
            )

            for department_seed in DEPARTMENTS:
                department = departments[department_seed["prefix"]]
                for index in range(1, 3):
                    await user_repo.create_with_db(
                        session,
                        {
                            "username": f"{department_seed['name']}管理员{index}",
                            "uid": f"{department_seed['prefix']}_admin_{index}",
                            "password_hash": AuthUtils.hash_password(DEFAULT_USER_PASSWORD),
                            "department_id": department.id,
                        },
                        default_role_code="admin",
                    )
                for index in range(1, department_seed["normal_count"] + 1):
                    await user_repo.create_with_db(
                        session,
                        {
                            "username": f"{department_seed['name']}用户{index}",
                            "uid": f"{department_seed['prefix']}_user_{index:02d}",
                            "password_hash": AuthUtils.hash_password(DEFAULT_USER_PASSWORD),
                            "department_id": department.id,
                        },
                    )
    finally:
        await pg_manager.close()


def main() -> int:
    load_project_env()
    try:
        asyncio.run(seed_initial_users())
    except SeedError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"初始化种子用户失败：{exc}", file=sys.stderr)
        return 1

    print(
        f"初始化完成：已创建超级管理员 {SUPERADMIN_NAME}（{SUPERADMIN_UID}）、"
        "3 个部门、6 个部门管理员和 14 个普通用户。"
    )
    print("超级管理员密码：zwj12138")
    print("部门管理员和普通用户默认密码：yuxi123456")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
