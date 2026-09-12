"""将旧 OA 部门树同步到当前平台，默认只生成 dry-run 计划。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from sqlalchemy import select

APP_ROOT = Path(__file__).resolve().parents[1]
for import_path in (APP_ROOT, APP_ROOT / "package"):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from scripts.migrate_oa_users import fetch_oa_access_code

LOGGER = logging.getLogger("oa_department_migration")


@dataclass(frozen=True)
class OaDepartment:
    """旧 OA 返回的部门树节点。"""

    oa_id: int
    oa_code: str
    name: str
    parent_oa_id: int | None


@dataclass(frozen=True)
class DepartmentAction:
    """一条可审阅的部门同步动作。"""

    action: str
    oa_id: int
    oa_code: str
    name: str
    department_id: int | None = None
    existing_oa_id: int | None = None
    parent_oa_id: int | None = None
    reason: str | None = None


def _flatten(nodes: list[dict], parent_oa_id: int | None = None) -> list[OaDepartment]:
    """把旧 OA 的嵌套树展平，同时保留父子关系。"""
    result: list[OaDepartment] = []
    for node in nodes:
        oa_id = int(node.get("id") or 0)
        oa_code = str(node.get("treeCode") or "").strip()
        name = str(node.get("treeName") or "").strip()
        if oa_id and oa_code and name:
            result.append(OaDepartment(oa_id, oa_code, name, parent_oa_id))
        elif oa_id or oa_code or name:
            raise RuntimeError("旧 OA 部门节点缺少 id、treeCode 或 treeName")
        result.extend(_flatten(node.get("children") or [], oa_id or parent_oa_id))
    return result


def fetch_oa_departments(url: str, code: str, timeout: float) -> list[OaDepartment]:
    """调用旧 OA 部门树接口并展平返回结果。"""
    request = Request(url, data=b"{}", headers={"Code": code}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if payload.get("status") != 1:
        raise RuntimeError("旧 OA 部门接口返回失败")
    return _flatten(payload.get("data") or [])


def build_department_actions(
    oa_departments: list[OaDepartment], existing: list[tuple[int, str, int | None, str | None, int | None]]
) -> list[DepartmentAction]:
    """按 OA 父子路径生成创建或编码回填计划。"""
    # 旧 OA 顶层节点在当前平台统一挂到集团根节点（id=1）下。
    existing_by_parent = {(parent_id, name.strip()): (department_id, oa_code, oa_id) for department_id, name, parent_id, oa_code, oa_id in existing}
    mapped_department_ids: dict[int, int] = {}
    seen_codes: set[str] = set()
    actions: list[DepartmentAction] = []
    for department in oa_departments:
        if department.oa_code in seen_codes:
            actions.append(DepartmentAction("skip", department.oa_id, department.oa_code, department.name, parent_oa_id=department.parent_oa_id, reason="OA 部门编码重复"))
            continue
        seen_codes.add(department.oa_code)
        current_parent_id = 1 if department.parent_oa_id is None else mapped_department_ids.get(department.parent_oa_id)
        if current_parent_id is None:
            actions.append(DepartmentAction("skip", department.oa_id, department.oa_code, department.name, parent_oa_id=department.parent_oa_id, reason="父部门未映射"))
            continue
        current = existing_by_parent.get((current_parent_id, department.name))
        if current is not None:
            department_id, current_code, current_oa_id = current
            mapped_department_ids[department.oa_id] = department_id
            if (current_code and current_code != department.oa_code) or (current_oa_id and current_oa_id != department.oa_id):
                actions.append(DepartmentAction("conflict", department.oa_id, department.oa_code, department.name, department_id, current_oa_id, department.parent_oa_id, "当前部门已有不同 OA 标识"))
            elif current_code and current_oa_id:
                actions.append(DepartmentAction("skip", department.oa_id, department.oa_code, department.name, department_id=department_id, parent_oa_id=department.parent_oa_id, reason="部门标识已存在"))
            else:
                actions.append(DepartmentAction("update_identity", department.oa_id, department.oa_code, department.name, department_id=department_id, existing_oa_id=current_oa_id, parent_oa_id=department.parent_oa_id))
        else:
            actions.append(DepartmentAction("create", department.oa_id, department.oa_code, department.name, parent_oa_id=department.parent_oa_id))
            # dry-run 中也记录将创建的节点，使子节点可按真实父路径继续匹配。
            mapped_department_ids[department.oa_id] = -department.oa_id
            existing_by_parent[(-department.oa_id, department.name)] = (-department.oa_id, department.oa_code, department.oa_id)
    return actions


async def load_database_departments() -> list[tuple[int, str, int | None, str | None, int | None]]:
    """读取当前部门树，供 dry-run 计划计算使用。"""
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_business import Department

    async with pg_manager.get_async_session_context() as session:
        rows = (await session.execute(select(Department.id, Department.name, Department.parent_id, Department.oa_department_code, Department.oa_department_id))).all()
    return list(rows)


async def apply_actions(actions: list[DepartmentAction]) -> None:
    """在一个事务中按 OA 父子关系创建部门。"""
    from yuxi.repositories.department_repository import DepartmentRepository
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_business import Department, ROOT_DEPARTMENT_ID

    async with pg_manager.get_async_session_context() as session:
        repository = DepartmentRepository(session)
        mapped_department_ids: dict[int, int] = {}
        for action in actions:
            if action.action == "conflict":
                raise RuntimeError(f"部门编码冲突：{action.name} ({action.oa_code})")
            if action.action == "skip":
                if action.department_id is not None:
                    # 已存在节点同样必须写入映射，供其子节点精确定位父部门。
                    mapped_department_ids[action.oa_id] = action.department_id
                continue
            parent_id = ROOT_DEPARTMENT_ID if action.parent_oa_id is None else mapped_department_ids.get(action.parent_oa_id)
            if parent_id is None:
                raise RuntimeError(f"执行期间父部门未映射：{action.name}")
            parent = await session.get(Department, parent_id)
            if parent is None:
                raise RuntimeError("当前平台集团根部门不存在")
            if action.action == "create":
                department = await repository.create_child(session, name=action.name, description="旧 OA 同步部门", parent=parent)
                department.oa_department_code = action.oa_code
                department.oa_department_id = action.oa_id
                await session.flush()
                LOGGER.info("创建旧 OA 部门：名称=%s，编码=%s", action.name, action.oa_code)
            else:
                department = await session.get(Department, action.department_id)
                if department is None:
                    raise RuntimeError(f"执行期间找不到待回填部门：{action.name}")
                department.oa_department_code = action.oa_code
                department.oa_department_id = action.oa_id
                await session.flush()
                LOGGER.info("回填旧 OA 部门标识：名称=%s，编码=%s，ID=%s", action.name, action.oa_code, action.oa_id)
            mapped_department_ids[action.oa_id] = department.id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="同步旧 OA 部门树")
    parser.add_argument("--url", default=os.getenv("OA_USER_MIGRATION_URL", "").replace("/DrugDevp/QueryUserPage", "/DrugDevp/QueryDepartmentTree"))
    parser.add_argument("--license-file", default=os.getenv("OA_USER_MIGRATION_LICENSE_FILE"))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--apply", action="store_true", help="提交数据库变更；默认 dry-run")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    if not args.url:
        LOGGER.error("请提供 --url 或 OA_USER_MIGRATION_URL")
        return 2
    from yuxi.storage.postgres.manager import pg_manager

    try:
        pg_manager.initialize()
        await pg_manager.create_business_tables()
        await pg_manager.ensure_business_schema()
        # 运行时 Code 只在内存中存在，避免通过命令行或环境变量泄露。
        code = (
            fetch_oa_access_code(
                args.url.replace("/DrugDevp/QueryDepartmentTree", "/DrugDevp/QueryUserPage"),
                args.license_file,
                args.timeout,
            )
            if args.license_file
            else None
        )
        if not code:
            LOGGER.error("请配置 OA_USER_MIGRATION_LICENSE_FILE")
            return 2
        actions = build_department_actions(await asyncio.to_thread(fetch_oa_departments, args.url, code, args.timeout), await load_database_departments())
        counts: dict[str, int] = {}
        for action in actions:
            counts[action.action] = counts.get(action.action, 0) + 1
        LOGGER.info("部门迁移计划：%s", counts)
        if args.apply:
            await apply_actions(actions)
            LOGGER.info("部门迁移事务已提交")
        return 0
    finally:
        await pg_manager.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
