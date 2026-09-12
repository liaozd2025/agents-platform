"""按固定顺序同步旧 OA 部门与在职用户，默认仅生成预演计划。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
import sys

APP_ROOT = Path(__file__).resolve().parents[1]
for import_path in (APP_ROOT, APP_ROOT / "package"):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from scripts import migrate_oa_departments as department_migration
from scripts import migrate_oa_users as user_migration

LOGGER = logging.getLogger("oa_data_sync")
USER_API_SUFFIX = "/DrugDevp/QueryUserPage"
DEPARTMENT_API_SUFFIX = "/DrugDevp/QueryDepartmentTree"


def build_department_url(user_url: str) -> str:
    """由旧 OA 用户接口推导同一服务的部门树接口地址。"""
    normalized_url = user_url.rstrip("/")
    if not normalized_url.endswith(USER_API_SUFFIX):
        raise ValueError("OA 用户接口地址必须以 /DrugDevp/QueryUserPage 结尾")
    return f"{normalized_url[: -len(USER_API_SUFFIX)]}{DEPARTMENT_API_SUFFIX}"


def count_actions(actions: list[object]) -> dict[str, int]:
    """汇总迁移动作，避免日志输出逐条部门或人员信息。"""
    counts: dict[str, int] = {}
    for action in actions:
        counts[action.action] = counts.get(action.action, 0) + 1
    return counts


def build_post_department_inputs(
    actions: list[department_migration.DepartmentAction],
) -> list[tuple[int, int | None]]:
    """在 dry-run 中模拟部门写入后的 OA 部门映射，供用户计划准确预演。"""
    department_inputs: list[tuple[int, int | None]] = []
    for action in actions:
        if action.action in {"conflict", "skip"} and action.department_id is None:
            continue
        # 新建部门尚未有真实主键，使用负数占位即可满足用户归属匹配，不会写入数据库。
        department_id = action.department_id if action.department_id is not None else -action.oa_id
        department_inputs.append((department_id, action.oa_id))
    return department_inputs


def parse_args() -> argparse.Namespace:
    """解析统一同步所需配置，运行时 Code 不接受命令行传入。"""
    parser = argparse.ArgumentParser(description="同步旧 OA 部门与在职用户，默认 dry-run")
    parser.add_argument("--url", default=os.getenv("OA_USER_MIGRATION_URL"))
    parser.add_argument("--license-file", default=os.getenv("OA_USER_MIGRATION_LICENSE_FILE"))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--company-code",
        default=os.getenv("OA_USER_MIGRATION_COMPANY_CODE", user_migration.DEFAULT_OA_COMPANY_CODE),
    )
    parser.add_argument("--apply", action="store_true", help="确认后提交部门和用户变更")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    """执行部门优先的同步编排，任一预检查失败时不继续后续写入。"""
    if not args.url:
        LOGGER.error("未配置 OA_USER_MIGRATION_URL 或 --url")
        return 2
    if not args.license_file:
        LOGGER.error("未配置 OA_USER_MIGRATION_LICENSE_FILE 或 --license-file")
        return 2

    from yuxi.storage.postgres.manager import pg_manager

    try:
        department_url = build_department_url(args.url)
        pg_manager.initialize()
        await pg_manager.create_business_tables()
        await pg_manager.ensure_business_schema()

        LOGGER.info("开始读取旧 OA 数据并生成部门同步计划")
        code = user_migration.fetch_oa_access_code(args.url, args.license_file, args.timeout)
        oa_departments = await asyncio.to_thread(
            department_migration.fetch_oa_departments, department_url, code, args.timeout
        )
        department_actions = department_migration.build_department_actions(
            oa_departments, await department_migration.load_database_departments()
        )
        department_counts = count_actions(department_actions)
        LOGGER.info("部门同步计划：%s", department_counts)
        if department_counts.get("conflict"):
            LOGGER.error("部门计划存在冲突，已停止，未写入部门或用户")
            return 1

        if args.apply:
            LOGGER.info("部门计划通过，开始写入部门")
            await department_migration.apply_actions(department_actions)
            # 部门提交后重新读取真实主键映射，用户写入不能依赖 dry-run 占位值。
            department_inputs, existing_users = await user_migration.load_database_inputs()
        else:
            LOGGER.info("当前为 dry-run，使用部门计划模拟用户归属映射")
            department_inputs = build_post_department_inputs(department_actions)
            _, existing_users = await user_migration.load_database_inputs()

        LOGGER.info("开始生成用户同步计划")
        oa_users = await asyncio.to_thread(user_migration.fetch_oa_users, args.url, code, args.timeout)
        user_actions = user_migration.build_migration_actions(
            oa_users, department_inputs, existing_users, args.company_code
        )
        user_counts = count_actions(user_actions)
        LOGGER.info("用户同步计划：%s", user_counts)
        if user_counts.get("conflict"):
            LOGGER.error("用户计划存在冲突，未写入用户")
            return 1

        if args.apply:
            LOGGER.info("用户计划通过，开始写入用户")
            await user_migration.apply_actions(user_actions)
            LOGGER.info("旧 OA 部门与用户同步完成")
        else:
            LOGGER.info("dry-run 完成，未写入部门或用户")
        return 0
    finally:
        await pg_manager.close()


def main() -> int:
    """配置命令行日志并启动异步同步流程。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
