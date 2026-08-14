import hashlib
import re
import time

from fastapi import HTTPException, status
from pypinyin import Style, lazy_pinyin
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from yuxi.storage.postgres.models_business import Department, User
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


async def get_or_create_external_department(
    db,
    department_name: str | None = None,
    department_description: str | None = None,
    default_department: str | None = None,
) -> Department | None:
    """获取或创建外部身份所属部门。"""
    processed_dept_name = department_name.strip()[:50] if department_name else None
    processed_dept_desc = department_description.strip()[:255] if department_description else None
    final_dept_name = processed_dept_name or default_department
    if not final_dept_name:
        return None

    result = await db.execute(select(Department).filter(Department.name == final_dept_name))
    department = result.scalar_one_or_none()
    if department:
        logger.info(f"Using existing department: {final_dept_name}")
        return department

    department = Department(
        name=final_dept_name,
        description=processed_dept_desc or f"{final_dept_name}部门",
    )
    db.add(department)
    try:
        await db.commit()
        await db.refresh(department)
        logger.info(f"Created external identity department: {final_dept_name}")
    except IntegrityError:
        await db.rollback()
        result = await db.execute(select(Department).filter(Department.name == final_dept_name))
        department = result.scalar_one_or_none()

    return department


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
