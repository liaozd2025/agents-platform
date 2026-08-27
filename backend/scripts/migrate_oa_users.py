"""将旧 OA 在职人员按 account 迁移到当前 users 表。

默认只生成迁移计划（dry-run）；只有传入 ``--apply`` 才会提交数据库事务。
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal, InvalidOperation
import json
import logging
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from sqlalchemy import select

APP_ROOT = Path(__file__).resolve().parents[1]
for import_path in (APP_ROOT, APP_ROOT / "package"):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

LOGGER = logging.getLogger("oa_user_migration")
DEFAULT_OA_COMPANY_CODE = "ZD"


@dataclass(frozen=True)
class OaUser:
    """旧 OA 返回的最小人员信息。"""

    account: str
    full_name: str
    department_name: str
    oa_department_id: int | None = None


@dataclass(frozen=True)
class MigrationAction:
    """一条可审阅的迁移动作。"""

    action: str
    account: str
    department_id: int | None = None
    reason: str | None = None
    uid: str | None = None
    # 仅用于管理界面展示的姓名，不能作为登录账号或 OA 身份匹配键。
    display_name: str | None = None


def build_oa_uid(company_code: str, account: str) -> str:
    """按 iframe SSO 使用的规则生成稳定 OA 身份键。"""
    return f"oa:{company_code.strip()}:{account.strip()}"


def build_migration_actions(
    oa_users: list[OaUser],
    departments: list[tuple[int, int | None]],
    existing_users: list[tuple],
    company_code: str = DEFAULT_OA_COMPANY_CODE,
) -> list[MigrationAction]:
    """根据 OA 人员、部门和现有用户生成不产生副作用的迁移计划。"""
    department_ids: dict[int, list[int]] = {}
    for department_id, oa_department_id in departments:
        if oa_department_id is not None:
            department_ids.setdefault(oa_department_id, []).append(department_id)
    existing = {
        row[1].strip(): (
            row[0],
            row[2],
            row[3] if len(row) > 3 else None,
            row[4] if len(row) > 4 else None,
        )
        for row in existing_users
    }
    stable_uid_owners = {
        row[3]: row[0] for row in existing_users if len(row) > 3 and row[3]
    }
    seen: set[str] = set()
    actions: list[MigrationAction] = []

    for oa_user in oa_users:
        account = oa_user.account.strip()
        full_name = oa_user.full_name.strip()
        if not account:
            actions.append(MigrationAction("skip", account, reason="账号为空"))
            continue
        if not full_name:
            actions.append(MigrationAction("skip", account, reason="姓名为空"))
            continue
        if account in seen:
            actions.append(MigrationAction("skip", account, reason="OA 账号重复"))
            continue
        seen.add(account)
        if oa_user.oa_department_id is None:
            actions.append(MigrationAction("skip", account, reason="部门 ID 为空或格式无效"))
            continue
        matched_departments = department_ids.get(oa_user.oa_department_id, [])
        if len(matched_departments) != 1:
            reason = "部门 ID 不存在" if not matched_departments else "部门 ID 重复"
            actions.append(MigrationAction("skip", account, reason=reason))
            continue
        department_id = matched_departments[0]
        stable_uid = build_oa_uid(company_code, account)
        uid_owner = stable_uid_owners.get(stable_uid)
        current = existing.get(account)
        if uid_owner is not None and (current is None or uid_owner != current[0]):
            actions.append(MigrationAction("conflict", account, reason="稳定 OA 身份键已被其他用户占用"))
            continue
        if current is not None:
            needs_department_update = current[1] != department_id
            needs_uid_update = current[2] != stable_uid
            needs_display_name_update = current[3] != full_name
            if needs_department_update or needs_uid_update or needs_display_name_update:
                # 同一用户的多个字段合并为一次动作，避免后续写入覆盖前一项更新。
                actions.append(
                    MigrationAction(
                        "update_identity",
                        account,
                        department_id=department_id if needs_department_update else None,
                        uid=stable_uid if needs_uid_update else None,
                        display_name=full_name if needs_display_name_update else None,
                    )
                )
            else:
                actions.append(MigrationAction("skip", account, department_id=department_id, reason="用户及部门已存在"))
            continue
        actions.append(
            MigrationAction(
                "create",
                account,
                department_id=department_id,
                uid=stable_uid,
                display_name=full_name,
            )
        )
    return actions


def fetch_oa_users(url: str, code: str, timeout: float) -> list[OaUser]:
    """调用旧 OA 分页接口并提取在职人员。"""
    request = Request(
        url,
        data=json.dumps({"userStateCode": 2, "page": 1, "pageSize": 99999}).encode(),
        headers={"Code": code, "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if payload.get("status") != 1:
        raise RuntimeError("旧 OA 人员接口返回失败状态")
    records = payload.get("data", {}).get("pageDatas", [])
    return [
        OaUser(
            account=str(item.get("account") or ""),
            # sys_user.nick_name 是旧 OA 的真实姓名来源；旧接口字段使用驼峰命名。
            # 兼容部分历史响应仍返回 fullName 或 userName 的情况。
            full_name=str(item.get("nickName") or item.get("fullName") or item.get("userName") or ""),
            department_name=str(item.get("appointmentDepartmentName") or ""),
            oa_department_id=normalize_oa_department_id(item.get("departmentId")),
        )
        for item in records
    ]


def normalize_oa_department_id(value: object) -> int | None:
    """将旧 OA 的整数或 xx.0 浮点形式部门 ID 规范化为整数。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        decimal_value = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    if decimal_value != decimal_value.to_integral_value() or decimal_value <= 0:
        return None
    return int(decimal_value)


def fetch_oa_access_code(user_page_url: str, license_file: str, timeout: float) -> str:
    """从本地授权文件临时换取本次旧 OA 查询所需的 Code。"""
    suffix = "/DrugDevp/QueryUserPage"
    if not user_page_url.rstrip("/").endswith(suffix):
        raise ValueError("OA 用户接口地址必须以 /DrugDevp/QueryUserPage 结尾，才能推导 GetCode 地址")

    # 授权文件仅在内存中用于本次请求，禁止记录内容或写回配置。
    secret_key = Path(license_file).read_text(encoding="utf-8").strip()
    if not secret_key:
        raise ValueError("OA 授权文件为空")
    code_url = f"{user_page_url.rstrip('/')[:-len(suffix)]}/SimpleAuth/GetCode"
    request = Request(
        code_url,
        data=json.dumps({"SecretKey": secret_key}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if payload.get("status") != 1 or not payload.get("data"):
        raise RuntimeError("旧 OA GetCode 接口返回失败")
    LOGGER.info("已通过本地授权文件获取本次迁移所需的旧 OA Code")
    return str(payload["data"])


def resolve_oa_access_code(args: argparse.Namespace) -> str | None:
    """仅使用本地授权文件换取本次运行所需的临时 Code。"""
    if not args.license_file:
        return None
    return fetch_oa_access_code(args.url, args.license_file, args.timeout)


async def apply_actions(actions: list[MigrationAction]) -> None:
    """在单个事务中执行创建和部门更新动作。"""
    from yuxi.repositories.user_repository import UserRepository
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_business import User
    from yuxi.utils.auth_utils import AuthUtils

    async with pg_manager.get_async_session_context() as session:
        repository = UserRepository()
        for action in actions:
            if action.action in {"update_department", "update_uid", "update_identity"}:
                user = await session.scalar(select(User).where(User.username == action.account, User.is_deleted == 0))
                if user is None:
                    raise RuntimeError(f"执行期间找不到用户：{action.account}")
                if action.department_id is not None:
                    user.department_id = action.department_id
                if action.uid is not None:
                    user.uid = action.uid
                if action.display_name is not None:
                    user.display_name = action.display_name
                # 日志只记录更新类别，避免输出人员姓名等敏感信息。
                LOGGER.info("更新 OA 用户资料：账号=%s，更新部门=%s，更新稳定UID=%s，更新展示姓名=%s", action.account, action.department_id is not None, action.uid is not None, action.display_name is not None)
            elif action.action == "conflict":
                raise RuntimeError(f"稳定 OA 身份键冲突：{action.account}")
            elif action.action == "create":
                await repository.create_with_db(
                    session,
                    {
                        "username": action.account,
                        "uid": action.uid,
                        "display_name": action.display_name,
                        "password_hash": AuthUtils.hash_password(secrets.token_urlsafe(32)),
                        "department_id": action.department_id,
                    },
                    default_role_code="user",
                )
                LOGGER.info("创建 OA 用户：账号=%s，部门ID=%s，已写入展示姓名", action.account, action.department_id)


async def load_database_inputs() -> tuple[list[tuple[int, int | None]], list[tuple[int, str, int | None, str | None, str | None]]]:
    """读取部门和未删除用户，供计划计算使用。"""
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_business import Department, User

    async with pg_manager.get_async_session_context() as session:
        department_rows = (await session.execute(select(Department.id, Department.oa_department_id))).all()
        user_rows = (
            await session.execute(
                select(User.id, User.username, User.department_id, User.uid, User.display_name).where(User.is_deleted == 0)
            )
        ).all()
    return list(department_rows), list(user_rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="迁移旧 OA 在职用户到当前 users 表")
    parser.add_argument("--url", default=os.getenv("OA_USER_MIGRATION_URL"))
    parser.add_argument(
        "--license-file",
        default=os.getenv("OA_USER_MIGRATION_LICENSE_FILE"),
        help="旧 OA license.lic 的容器内只读路径",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--company-code",
        default=os.getenv("OA_USER_MIGRATION_COMPANY_CODE", DEFAULT_OA_COMPANY_CODE),
        help="与 iframe SSO 一致的 OA 公司编码，默认读取 OA_USER_MIGRATION_COMPANY_CODE 或使用 ZD",
    )
    parser.add_argument("--apply", action="store_true", help="提交数据库变更；默认仅 dry-run")
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
        code = resolve_oa_access_code(args)
        if not code:
            LOGGER.error("请配置 OA_USER_MIGRATION_LICENSE_FILE")
            return 2
        oa_users = fetch_oa_users(args.url, code, args.timeout)
        departments, existing_users = await load_database_inputs()
        actions = build_migration_actions(oa_users, departments, existing_users, args.company_code)
        counts: dict[str, int] = {}
        for action in actions:
            counts[action.action] = counts.get(action.action, 0) + 1
        LOGGER.info("迁移计划：%s", counts)
        if counts.get("conflict"):
            LOGGER.error("检测到稳定 OA 身份键冲突，本次不允许写入")
            return 1
        if args.apply:
            await apply_actions(actions)
            LOGGER.info("迁移事务已提交")
        return 0
    finally:
        await pg_manager.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
