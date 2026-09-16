"""OA 自定义 token 与 Yuxi 登录态的安全交换。"""

import json
import os
import re
import secrets
import urllib.parse
from typing import Any

import httpx
import jwt
from fastapi import HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from yuxi.permissions.authorization import build_authorization_context
from yuxi.repositories.user_repository import UserRepository
from yuxi.services.operation_log_service import log_operation
from yuxi.services.user_identity_service import build_unique_external_username, resolve_external_department
from yuxi.services.user_role_service import serialize_user
from yuxi.storage.postgres.models_business import User
from yuxi.utils.auth_utils import AuthUtils
from yuxi.utils.datetime_utils import utc_now_naive
from yuxi.utils.logging_config import logger

MAX_OA_TOKEN_LENGTH = 16_384
MAX_OA_ACCOUNT_LENGTH = 64
ACTIVE_OA_USER_STATE = "service"
LOCAL_OA_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
# 本地账号按工号反查 OA 资料：只认 6~12 位纯数字工号，避免拿用户名去猜他人档案
EMPLOYEE_ACCOUNT_PATTERN = re.compile(r"[0-9]{6,12}")
# 反查发生在登录主链路上，超时必须短于 SSO 的 10s，避免拖慢登录
OA_PROFILE_LOOKUP_TIMEOUT = 3.0


class OASSOConfig(BaseModel):
    """OA 自定义 SSO 配置。"""

    enabled: bool = False
    userinfo_url: str = ""
    company_code: str = ""

    @classmethod
    def from_env(cls) -> "OASSOConfig":
        """从环境变量读取 OA SSO 配置。"""
        return cls(
            enabled=os.environ.get("OA_SSO_ENABLED", "false").strip().lower() == "true",
            userinfo_url=os.environ.get("OA_SSO_USERINFO_URL", "").strip(),
            company_code=os.environ.get("OA_SSO_COMPANY_CODE", "").strip(),
        )

    def is_configured(self) -> bool:
        """检查 OA SSO 配置是否可用。"""
        if not self.enabled or not self.company_code:
            return False
        try:
            parsed = urllib.parse.urlsplit(self.userinfo_url)
        except ValueError:
            return False
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return False
        if parsed.scheme == "https":
            return True
        environment = os.environ.get("YUXI_ENV", "development").strip().lower()
        return environment == "development" and parsed.scheme == "http" and parsed.hostname in LOCAL_OA_HOSTS


class OAAccountLoginConfig(BaseModel):
    """父项目仅提供账号时使用的临时 OA 登录配置。"""

    enabled: bool = False
    login_url: str = ""
    company_code: str = ""

    @classmethod
    def from_env(cls) -> "OAAccountLoginConfig":
        """从环境变量读取 account 换票配置。"""
        return cls(
            enabled=os.environ.get("OA_ACCOUNT_LOGIN_ENABLED", "false").strip().lower() == "true",
            login_url=os.environ.get("OA_ACCOUNT_LOGIN_URL", "").strip(),
            company_code=os.environ.get("OA_ACCOUNT_LOGIN_COMPANY_CODE", "").strip(),
        )

    def is_configured(self) -> bool:
        """校验账号换票配置。"""
        environment = os.environ.get("YUXI_ENV", "development").strip().lower()
        if not self.enabled or not self.company_code:
            return False
        try:
            parsed = urllib.parse.urlsplit(self.login_url)
        except ValueError:
            return False
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return False
        if parsed.scheme == "https":
            return True
        return environment == "development" and parsed.scheme == "http" and parsed.hostname in LOCAL_OA_HOSTS


def _compose_job_level(level: str | None, grade: str | None) -> str | None:
    """把职级与职等拼成 USER.md 用的展示文本，如“11（基层）”。

    只有一项时单独展示，两项都缺返回 ``None``，由调用方决定不写这一行。
    """
    if level and grade:
        return f"{level}（{grade}）"[:100]
    return level or grade or None


class OAIdentity(BaseModel):
    """OA 用户接口验证后的最小可信身份。"""

    company_code: str
    account: str
    full_name: str
    department_name: str | None = None
    department_code: str | None = None

    # 以下字段只作为登录响应的展示资料透出给前端，不参与身份匹配、不写入数据库；
    # OA 未返回或返回不可解析时为 None（缺字段不能影响登录）。
    station_name: str | None = None  # 主岗岗位名，如“中级前端程序员”
    job_level_name: str | None = None  # 职级，如“11”
    job_grade_name: str | None = None  # 职等，如“基层”
    business_division_name: str | None = None  # 所属业务分部，如“信息中心”
    superior_name: str | None = None  # 直接上级姓名
    company_full_name: str | None = None  # 法人公司全称
    induction_date: str | None = None  # 入职日期，保持 OA 原始字符串格式
    sex_name: str | None = None  # 性别文本，如“男”
    mobile_number: str | None = None  # 手机号；仅透出，禁止写入 users.phone_number
    avatar_url: str | None = None  # OA 头像相对地址（resources/images/...）
    avatar_download_url: str | None = None  # OA 头像下载相对地址（Files/guest/...）

    @property
    def uid(self) -> str:
        """返回 Yuxi 中稳定的 OA 身份键。"""
        return f"oa:{self.company_code}:{self.account}"

    @property
    def job_level_text(self) -> str | None:
        """USER.md 使用的职级展示文本（职级（职等））。"""
        return _compose_job_level(self.job_level_name, self.job_grade_name)


oa_sso_config = OASSOConfig.from_env()
oa_account_login_config = OAAccountLoginConfig.from_env()


def extract_oa_token_account(token: str) -> str:
    """从 OA 双 JWT 载荷取出一致账号，最终身份仍以 OA 接口为准。"""
    if not isinstance(token, str) or not token or len(token) > MAX_OA_TOKEN_LENGTH:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OA 登录凭证无效")

    token_parts = token.split("|")
    if len(token_parts) != 2:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OA 登录凭证格式无效")

    accounts = []
    for token_part in token_parts:
        try:
            claims = jwt.decode(token_part, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OA 登录凭证格式无效") from exc
        data = claims.get("data")
        account = str(data.get("account", "")).strip() if isinstance(data, dict) else ""
        if not account or len(account) > 64:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OA 登录凭证缺少账号")
        accounts.append(account)

    if accounts[0] != accounts[1]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OA 登录凭证账号不一致")
    return accounts[0]


def _primary_job(user_data: dict[str, Any]) -> dict[str, Any] | None:
    jobs = user_data.get("userJobInformationDtos")
    if not isinstance(jobs, list):
        return None
    valid_jobs = [job for job in jobs if isinstance(job, dict)]
    if not valid_jobs:
        return None

    def sort_key(job: dict[str, Any]) -> float:
        try:
            return float(job.get("pagingSort"))
        except (TypeError, ValueError):
            return float("inf")

    return min(valid_jobs, key=sort_key)


def _text(value: Any, limit: int) -> str | None:
    """把 OA 字段归一化为去空白且限长的可选文本，空值一律返回 None。"""
    text = str(value).strip() if value is not None else ""
    return text[:limit] or None


def _first_avatar_entry(user_data: dict[str, Any]) -> dict[str, Any] | None:
    """从 OA 头像字段取第一条记录。

    ``photoGraphFileName`` 是「JSON 字符串」而不是 JSON 对象（网关原样透传），
    因此必须显式解析；解析失败只记日志并返回 None，绝不能让登录因此失败。
    """
    raw = user_data.get("photoGraphFileName")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        entries = json.loads(raw)
    except ValueError:
        logger.warning("OA 头像字段不是合法 JSON，已忽略该字段")
        return None
    if not isinstance(entries, list):
        logger.warning("OA 头像字段不是数组，已忽略该字段")
        return None
    return next((entry for entry in entries if isinstance(entry, dict)), None)


def _oa_profile_payload(identity: OAIdentity) -> dict[str, str | None]:
    """整理登录响应里的 OA 展示资料（别名 ``oa_profile``），不落库、不参与鉴权。

    这里给的是 OA 原始字段（职级就是 ``11``）；写入用户画像时用 ``job_level_text``
    组合成「11（基层）」，两者的口径差异属有意保留。
    """
    return {
        "station_name": identity.station_name,
        "job_level_name": identity.job_level_name,
        "job_grade_name": identity.job_grade_name,
        "business_division_name": identity.business_division_name,
        "superior_name": identity.superior_name,
        "company_full_name": identity.company_full_name,
        "induction_date": identity.induction_date,
        "sex_name": identity.sex_name,
        "mobile_number": identity.mobile_number,
        "avatar_url": identity.avatar_url,
        "avatar_download_url": identity.avatar_download_url,
    }


async def _request_oa_user_data(account: str, token: str | None, timeout: float = 10.0) -> dict[str, Any]:
    """请求 OA 用户信息接口并返回 ``data`` 段。

    只负责传输与响应外壳判断，账号、公司编码、在职状态等语义校验由调用方完成。
    ``token`` 为空时不带 Authorization 头（OA 资料反查发生在本地账号登录链路，没有 OA 凭证）。
    """
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            response = await client.get(oa_sso_config.userinfo_url, params={"Account": account}, headers=headers)
    except httpx.HTTPError as exc:
        logger.error(f"OA user info request failed: {type(exc).__name__}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "OA 用户服务暂不可用") from exc

    if response.status_code in {status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN}:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OA 登录凭证已失效")
    if response.status_code != status.HTTP_200_OK:
        logger.error(f"OA user info request returned status {response.status_code}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "OA 用户服务返回异常")

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "OA 用户服务返回无效数据") from exc
    user_data = payload.get("data") if isinstance(payload, dict) and payload.get("status") == 1 else None
    if not isinstance(user_data, dict):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OA 登录凭证已失效")
    return user_data


async def fetch_oa_identity(token: str, account: str) -> OAIdentity:
    """携带 OA token 请求固定用户接口并返回可信身份。"""
    user_data = await _request_oa_user_data(account, token)

    returned_account = str(user_data.get("account", "")).strip()
    company_code = str(user_data.get("companyCode", "")).strip()
    if returned_account != account:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OA 用户账号校验失败")
    if company_code != oa_sso_config.company_code:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "OA 用户不属于允许接入的公司")
    if user_data.get("userStateCode") != ACTIVE_OA_USER_STATE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "OA 用户不是在职状态")

    full_name = str(user_data.get("fullName") or user_data.get("userName") or "").strip() or account
    primary_job = _primary_job(user_data) or {}
    department_name = str(primary_job.get("appointmentDepartmentName") or "").strip()[:50] or None
    department_code = str(primary_job.get("appointmentDepartmentCode") or "").strip()[:64] or None
    avatar_entry = _first_avatar_entry(user_data) or {}
    # 展示资料随登录响应一起给前端；字段缺失只表现为 None，不做任何兜底默认值
    identity = OAIdentity(
        company_code=company_code,
        account=account,
        full_name=full_name[:100],
        department_name=department_name,
        department_code=department_code,
        station_name=_text(primary_job.get("appointmentStationName"), 100),
        job_level_name=_text(primary_job.get("jobLevelName"), 50),
        job_grade_name=_text(primary_job.get("jobGradeName"), 50),
        business_division_name=_text(primary_job.get("businessDivisionName"), 100),
        superior_name=_text(primary_job.get("superiorPostLeaderAccountName"), 100),
        company_full_name=_text(primary_job.get("companyFullName") or user_data.get("companyName"), 200),
        induction_date=_text(user_data.get("inductionDate"), 32),
        sex_name=_text(user_data.get("sexName"), 16),
        mobile_number=_text(user_data.get("mobileNumber"), 32),
        avatar_url=_text(avatar_entry.get("url"), 512),
        avatar_download_url=_text(avatar_entry.get("downUrl"), 512),
    )
    # 统一用 f-string：本项目 logger 包装器不解析 %-占位符，传参会原样打印
    logger.info(f"OA 用户资料已投影展示字段：uid={identity.uid}")
    return identity


async def backfill_local_user_oa_profile(db, user: User) -> bool:
    """按工号为本地账号反查 OA 资料，补齐展示姓名、岗位与职级。

    触发条件（全部满足才发起请求）：OA 用户接口已配置、用户不是 OA 身份（uid 不以 ``oa:`` 开头）、
    登录账号形如工号（6~12 位纯数字）。反查结果必须账号一致、公司编码一致且为在职状态才采纳；
    任何一步失败只记日志并返回 ``False``，绝不阻塞登录。只写 ``display_name``、``oa_station_name``
    与 ``oa_job_level_name``，不触碰账号、UID、部门与权限。

    ``db`` 是调用方提供的事务会话：本函数只改内存对象、不提交，改动由调用方随登录事务一并提交，
    保留该参数是为了让调用点显式看到写入所属的事务边界。
    """
    account = (user.username or "").strip()
    if not oa_sso_config.is_configured():
        return False
    if user.uid.startswith("oa:"):
        # OA 身份的资料由 SSO 登录链路维护，这里不再反查，避免两条链路互相覆盖
        return False
    if not EMPLOYEE_ACCOUNT_PATTERN.fullmatch(account):
        return False

    try:
        user_data = await _request_oa_user_data(account, None, timeout=OA_PROFILE_LOOKUP_TIMEOUT)
    except HTTPException as exc:
        logger.warning(f"OA 岗位反查未成功，跳过本次补全：account={account} status={exc.status_code}")
        return False

    if str(user_data.get("account", "")).strip() != account:
        logger.warning(f"OA 岗位反查账号不一致，跳过本次补全：account={account}")
        return False
    if str(user_data.get("companyCode", "")).strip() != oa_sso_config.company_code:
        logger.warning(f"OA 岗位反查公司编码不一致，跳过本次补全：account={account}")
        return False
    if user_data.get("userStateCode") != ACTIVE_OA_USER_STATE:
        logger.info(f"OA 岗位反查命中非在职状态，跳过本次补全：account={account}")
        return False

    full_name = str(user_data.get("fullName") or user_data.get("userName") or "").strip()
    primary_job = _primary_job(user_data) or {}
    station_name = _text(primary_job.get("appointmentStationName"), 100)
    job_level_name = _compose_job_level(
        _text(primary_job.get("jobLevelName"), 50),
        _text(primary_job.get("jobGradeName"), 50),
    )

    updated = False
    if full_name and user.display_name != full_name[:100]:
        user.display_name = full_name[:100]
        updated = True
    if station_name and user.oa_station_name != station_name:
        user.oa_station_name = station_name
        updated = True
    if job_level_name and user.oa_job_level_name != job_level_name:
        user.oa_job_level_name = job_level_name
        updated = True
    if updated:
        logger.info(
            f"OA 岗位反查已补齐本地账号资料：user_id={user.id} station={station_name} job_level={job_level_name}"
        )
    return updated


async def _complete_oa_login(
    identity: OAIdentity, db, request: Request | None, department=None, operation: str = "OA SSO 登录"
) -> dict[str, Any]:
    """按已验证的 OA 身份复用本地用户并签发 Yuxi 登录态。"""
    user_repo = UserRepository()

    result = await db.execute(select(User).where(User.uid == identity.uid))
    user = result.scalar_one_or_none()
    if user and user.is_deleted:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该 Yuxi 账户已注销")

    if user:
        user.last_login = utc_now_naive()
        # OA 返回的姓名是用户展示信息的唯一来源，不参与账号和 UID 身份匹配。
        if user.display_name != identity.full_name:
            user.display_name = identity.full_name
            logger.info(f"OA 登录已同步用户展示姓名：user_id={user.id}")
        # 岗位与职级供 USER.md 用户资料使用；OA 未返回时保留既有值，避免把已有资料清空。
        if identity.station_name and user.oa_station_name != identity.station_name:
            user.oa_station_name = identity.station_name
            logger.info(f"OA 登录已同步用户岗位：user_id={user.id}")
        if identity.job_level_text and user.oa_job_level_name != identity.job_level_text:
            user.oa_job_level_name = identity.job_level_text
            logger.info(f"OA 登录已同步用户职级：user_id={user.id}")
        if department:
            user.department_id = department.id
        await db.commit()
        await db.refresh(user)
    else:
        username = await build_unique_external_username(db, identity.full_name, identity.uid)
        user = await user_repo.create_with_db(
            db,
            {
                "username": username,
                "display_name": identity.full_name,
                "uid": identity.uid,
                # 岗位与职级供 USER.md 用户资料使用；手机号与头像不取 OA 值：
                # 手机号在本地有唯一索引、头像允许用户自行上传，二者只在登录响应 oa_profile 里透出。
                "oa_station_name": identity.station_name,
                "oa_job_level_name": identity.job_level_text,
                "phone_number": None,
                "avatar": None,
                "password_hash": AuthUtils.hash_password(secrets.token_urlsafe(32)),
                "department_id": department.id if department else None,
                "last_login": utc_now_naive(),
            },
        )
        try:
            await db.commit()
            await db.refresh(user)
        except IntegrityError as exc:
            await db.rollback()
            result = await db.execute(select(User).where(User.uid == identity.uid, User.is_deleted == 0))
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status.HTTP_409_CONFLICT, "OA 用户创建冲突，请重试") from exc

    user = await user_repo.get_by_id_with_db(db, user.id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OA 用户不存在")

    await log_operation(db, user.id, operation, request=request)
    return {
        "access_token": AuthUtils.create_access_token({"sub": str(user.id)}),
        "token_type": "bearer",
        "user_id": user.id,
        **serialize_user(user, department.name if department else None),
        "effective_permissions": list(build_authorization_context(user).effective_permissions),
        # OA 展示资料随响应透出；其中姓名与岗位同时落库供 USER.md 使用，手机号与头像不落库
        "oa_profile": _oa_profile_payload(identity),
    }


async def exchange_oa_token_handler(token: str, db, request: Request | None = None) -> dict[str, Any]:
    """验证 OA token，匹配本地用户并签发 Yuxi token。"""
    if not oa_sso_config.is_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OA 免登录未配置")

    account = extract_oa_token_account(token)
    identity = await fetch_oa_identity(token, account)
    department = await resolve_external_department(db, identity.department_name)
    return await _complete_oa_login(identity, db, request, department)


async def exchange_oa_account_handler(account: str, db, request: Request | None = None) -> dict[str, Any]:
    """使用父项目提供的账号换取 OA 凭证并建立临时内网登录态。"""
    if not oa_account_login_config.is_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OA 账号登录未配置")
    # 账号只是父页面传入的待校验线索，必须使用换到的 OA token 查询用户信息后才能建立本地登录态。
    if not oa_sso_config.is_configured() or oa_sso_config.company_code != oa_account_login_config.company_code:
        logger.error("OA account login requires a matching OA user info configuration")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OA 账号身份校验未配置")

    normalized_account = account.strip() if isinstance(account, str) else ""
    if not normalized_account or len(normalized_account) > MAX_OA_ACCOUNT_LENGTH:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OA 账号无效")

    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
            response = await client.post(
                oa_account_login_config.login_url,
                json={
                    "account": normalized_account,
                    "deviceId": "H5",
                    "companyCode": oa_account_login_config.company_code,
                    "loginType": "8",
                },
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        logger.error(f"OA account login request failed: {type(exc).__name__}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "OA 账号登录服务暂不可用") from exc

    if response.status_code != status.HTTP_200_OK:
        logger.error(f"OA account login returned status {response.status_code}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "OA 账号登录服务返回异常")
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "OA 账号登录服务返回无效数据") from exc

    response_data = payload.get("data") if isinstance(payload, dict) else None
    tokens = response_data
    if isinstance(response_data, dict) and isinstance(response_data.get("data"), dict):
        # 真实网关比 H5 的 Axios 返回值多一层 data，并用字符串 "1" 表示成功。
        if str(response_data.get("status")) != "1":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OA 账号登录失败")
        tokens = response_data["data"]
    if not isinstance(tokens, dict) or not all(
        isinstance(tokens.get(key), str) and tokens[key].strip() for key in ("oaToken", "saToken")
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OA 账号登录失败")

    returned_account = str(tokens.get("account") or "").strip()
    if returned_account and returned_account != normalized_account:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OA 用户账号校验失败")

    identity = await fetch_oa_identity(tokens["oaToken"].strip(), normalized_account)
    department = await resolve_external_department(db, identity.department_name)
    return await _complete_oa_login(identity, db, request, department, operation="OA账号代入登录")
