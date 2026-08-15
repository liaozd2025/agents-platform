import hashlib
import re
import time

from fastapi import HTTPException, status
from pypinyin import Style, lazy_pinyin
from sqlalchemy import select
from yuxi.storage.postgres.models_business import ROOT_DEPARTMENT_ID, Department, User
from yuxi.utils.logging_config import logger


def to_pinyin(text: str) -> str:
    return "".join(lazy_pinyin(text, style=Style.NORMAL))


def validate_username(username: str) -> tuple[bool, str]:
    if not username:
        return False, "用户名不能为空"
    if len(username) < 2:
        return False, "用户名长度不能少于2个字符"
    if len(username) > 20:
        return False, "用户名长度不能超过20个字符"
    if not re.match(r"^[一-龥a-zA-Z0-9_]+$", username):
        return False, "用户名只能包含中文、英文、数字和下划线"
    return True, ""


def generate_uid(username: str) -> str:
    uid = re.sub(r"[^a-zA-Z0-9_]", "", to_pinyin(username.strip()))
    if uid and uid[0].isdigit():
        uid = f"u{uid}"
    if len(uid) < 2:
        uid = f"user{hash(username) % 10000:04d}"
    return uid[:20].lower()


def generate_unique_uid(username: str, existing_uids: list[str]) -> str:
    base_uid = generate_uid(username)
    if base_uid not in existing_uids:
        return base_uid

    counter = 1
    while counter <= 9999:
        candidate = f"{base_uid}{counter}"
        if candidate not in existing_uids:
            return candidate
        counter += 1

    return f"{base_uid}{int(time.time()) % 10000}"


def is_valid_phone_number(phone: str) -> bool:
    if not phone:
        return False
    return bool(re.match(r"^1[3-9]\d{9}$", re.sub(r"[\s\-\(\)]", "", phone)))


def normalize_phone_number(phone: str) -> str:
    if not phone:
        return ""
    phone = re.sub(r"\D", "", phone)
    if len(phone) == 11 and phone.startswith("1"):
        return phone
    return phone


async def resolve_external_department(db, department_name: str | None) -> Department:
    """按外部身份部门名精确匹配已有组织节点，无法唯一定位时回落集团根。"""

    normalized_name = department_name.strip() if isinstance(department_name, str) else None
    matched = []
    if normalized_name:
        result = await db.execute(select(Department).where(Department.name == normalized_name))
        matched = list(result.scalars().all())

    if len(matched) == 1:
        logger.info(f"Using existing department: {normalized_name}")
        return matched[0]

    logger.warning(f"Department claim {department_name!r} matched {len(matched)} org nodes, falling back to group root")
    group_root = await db.get(Department, ROOT_DEPARTMENT_ID)
    if group_root is None:
        raise RuntimeError("集团根组织节点不存在")
    return group_root


async def build_unique_external_username(db, preferred_username: str, external_id: str) -> str:
    """为外部身份生成不冲突的显示用户名。"""
    base_username = preferred_username.strip() if preferred_username else ""
    if not base_username:
        base_username = f"external_{external_id[:8]}"

    result = await db.execute(select(User.id).filter(User.username == base_username))
    if result.scalar_one_or_none() is None:
        return base_username

    hash_suffix = hashlib.sha256(external_id.encode()).hexdigest()[:6]
    candidate = f"{base_username}-{hash_suffix}"
    result = await db.execute(select(User.id).filter(User.username == candidate))
    if result.scalar_one_or_none() is None:
        return candidate

    for index in range(2, 100):
        indexed_candidate = f"{candidate}-{index}"
        result = await db.execute(select(User.id).filter(User.username == indexed_candidate))
        if result.scalar_one_or_none() is None:
            return indexed_candidate

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="无法生成可用用户名，请联系管理员",
    )
