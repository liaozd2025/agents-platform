"""OA 自定义 token 交换的身份边界测试。"""

import json
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
import pytest_asyncio
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from yuxi.services import oa_sso_service
from yuxi.storage.postgres.models_business import (
    GROUP_NODE_TYPE,
    ROOT_DEPARTMENT_ID,
    Base,
    Department,
    Role,
    User,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def issue_oa_token(first_account: str = "oa-user-1", second_account: str | None = None) -> str:
    """签发仅用于测试的 OA 双 JWT token。"""
    second_account = second_account or first_account
    secret = "oa-unit-test-secret-at-least-32-bytes"
    first = jwt.encode({"data": {"account": first_account}}, secret, algorithm="HS256")
    second = jwt.encode({"data": {"account": second_account}}, secret, algorithm="HS256")
    return f"{first}|{second}"


async def test_production_account_login_accepts_account_only_config(monkeypatch):
    """生产环境只配置账号换票参数时，父项目可仅传账号发起登录。"""
    config = oa_sso_service.OAAccountLoginConfig(
        enabled=True,
        login_url="https://oa.example.test/login",
        company_code="TEST",
    )
    monkeypatch.setenv("YUXI_ENV", "production")
    assert config.is_configured() is True


@pytest_asyncio.fixture
async def oa_session():
    """创建 OA SSO 用户映射所需的最小数据库。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                Department(
                    id=ROOT_DEPARTMENT_ID,
                    name="集团",
                    node_type=GROUP_NODE_TYPE,
                    path=f"/{ROOT_DEPARTMENT_ID}/",
                ),
                Department(
                    id=2,
                    name="主部门",
                    parent_id=ROOT_DEPARTMENT_ID,
                    path=f"/{ROOT_DEPARTMENT_ID}/2/",
                ),
                Role(
                    code="user",
                    name="普通用户",
                    is_builtin=True,
                    is_active=True,
                    default_scope_type="self",
                ),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


async def test_oa_token_requires_matching_accounts():
    assert oa_sso_service.extract_oa_token_account(issue_oa_token()) == "oa-user-1"

    with pytest.raises(HTTPException, match="账号不一致"):
        oa_sso_service.extract_oa_token_account(issue_oa_token(second_account="another-user"))


async def test_oa_userinfo_is_authoritative_and_selects_primary_job(monkeypatch):
    token = issue_oa_token()
    captured_request = {}

    class FakeAsyncClient:
        def __init__(self, **options):
            captured_request["options"] = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, params, headers):
            captured_request.update({"url": url, "params": params, "headers": headers})
            return httpx.Response(
                200,
                json={
                    "status": 1,
                    "data": {
                        "companyCode": "TEST",
                        "account": "oa-user-1",
                        "fullName": "测试用户",
                        "userStateCode": "service",
                        "userJobInformationDtos": [
                            {
                                "pagingSort": 2,
                                "appointmentDepartmentCode": "secondary",
                                "appointmentDepartmentName": "兼职部门",
                            },
                            {
                                "pagingSort": 1,
                                "appointmentDepartmentCode": "primary",
                                "appointmentDepartmentName": "主部门",
                            },
                        ],
                    },
                },
            )

    monkeypatch.setattr(oa_sso_service.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "userinfo_url", "https://oa.example.test/userinfo")
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "company_code", "TEST")

    identity = await oa_sso_service.fetch_oa_identity(token, "oa-user-1")

    assert identity.uid == "oa:TEST:oa-user-1"
    assert identity.department_code == "primary"
    assert identity.department_name == "主部门"
    assert captured_request == {
        "options": {"follow_redirects": False, "timeout": 10.0},
        "url": "https://oa.example.test/userinfo",
        "params": {"Account": "oa-user-1"},
        "headers": {"Accept": "application/json", "Authorization": f"Bearer {token}"},
    }


async def test_oa_userinfo_projects_oa_profile_fields(monkeypatch):
    """OA 展示资料按主岗与头像 JSON 串投影，供登录响应透出（不写库）。"""
    token = issue_oa_token()

    class FakeAsyncClient:
        def __init__(self, **_options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return httpx.Response(
                200,
                json={
                    "status": 1,
                    "data": {
                        "companyCode": "TEST",
                        "account": "oa-user-1",
                        "fullName": "测试用户",
                        "userStateCode": "service",
                        "mobileNumber": "18670050179",
                        "inductionDate": "2024/10/28 00:00:00",
                        "sexName": "男",
                        "userJobInformationDtos": [
                            {
                                "pagingSort": 2,
                                "appointmentDepartmentCode": "secondary",
                                "appointmentDepartmentName": "兼职部门",
                                "appointmentStationName": "兼职岗位",
                            },
                            {
                                "pagingSort": 1,
                                "appointmentDepartmentCode": "primary",
                                "appointmentDepartmentName": "主部门",
                                "appointmentStationName": "中级前端程序员",
                                "jobLevelName": "11",
                                "jobGradeName": "基层",
                                "businessDivisionName": "信息中心",
                                "superiorPostLeaderAccountName": "上级姓名",
                                "companyFullName": "测试股份有限公司",
                            },
                        ],
                        # 线上该字段是「JSON 字符串」而不是对象，必须按字符串解析
                        "photoGraphFileName": json.dumps(
                            [
                                {
                                    "url": "resources/images/2024-10-29/a.jpg",
                                    "downUrl": "Files/guest/false/1.jpg",
                                }
                            ]
                        ),
                    },
                },
            )

    monkeypatch.setattr(oa_sso_service.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "userinfo_url", "https://oa.example.test/userinfo")
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "company_code", "TEST")

    identity = await oa_sso_service.fetch_oa_identity(token, "oa-user-1")

    # 展示资料只取主岗（pagingSort 最小），兼职岗不能覆盖
    assert identity.station_name == "中级前端程序员"
    assert identity.job_level_name == "11"
    assert identity.job_grade_name == "基层"
    assert identity.business_division_name == "信息中心"
    assert identity.superior_name == "上级姓名"
    assert identity.company_full_name == "测试股份有限公司"
    assert identity.induction_date == "2024/10/28 00:00:00"
    assert identity.sex_name == "男"
    assert identity.mobile_number == "18670050179"
    assert identity.avatar_url == "resources/images/2024-10-29/a.jpg"
    assert identity.avatar_download_url == "Files/guest/false/1.jpg"


@pytest.mark.parametrize("avatar_payload", ["not-json", "{}", "[]", "", 123, None])
async def test_oa_userinfo_tolerates_unusable_avatar_payload(monkeypatch, avatar_payload):
    """头像字段非法、结构不符或缺失时登录照常完成，只把头像一个置空。"""

    class FakeAsyncClient:
        def __init__(self, **_options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return httpx.Response(
                200,
                json={
                    "status": 1,
                    "data": {
                        "companyCode": "TEST",
                        "account": "oa-user-1",
                        "fullName": "测试用户",
                        "userStateCode": "service",
                        "photoGraphFileName": avatar_payload,
                    },
                },
            )

    monkeypatch.setattr(oa_sso_service.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "userinfo_url", "https://oa.example.test/userinfo")
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "company_code", "TEST")

    identity = await oa_sso_service.fetch_oa_identity(issue_oa_token(), "oa-user-1")

    assert identity.full_name == "测试用户"
    assert identity.avatar_url is None
    assert identity.avatar_download_url is None
    # 展示资料缺字段不能抛出异常，也不做兜底默认值
    assert identity.station_name is None
    assert identity.mobile_number is None


@pytest.mark.parametrize(
    ("account", "company_code", "user_state", "expected_status"),
    [
        ("another-user", "TEST", "service", 401),
        ("oa-user-1", "OTHER", "service", 403),
        ("oa-user-1", "TEST", "left", 403),
    ],
)
async def test_oa_userinfo_rejects_identity_mismatch(monkeypatch, account, company_code, user_state, expected_status):

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return httpx.Response(
                200,
                json={
                    "status": 1,
                    "data": {
                        "account": account,
                        "companyCode": company_code,
                        "fullName": "测试用户",
                        "userStateCode": user_state,
                    },
                },
            )

        def __init__(self, **_options):
            pass

    monkeypatch.setattr(oa_sso_service.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "userinfo_url", "https://oa.example.test/userinfo")
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "company_code", "TEST")

    with pytest.raises(HTTPException) as exc_info:
        await oa_sso_service.fetch_oa_identity(issue_oa_token(), "oa-user-1")
    assert exc_info.value.status_code == expected_status


async def test_oa_exchange_creates_one_local_user_and_issues_yuxi_token(monkeypatch, oa_session):
    identity = oa_sso_service.OAIdentity(
        company_code="TEST",
        account="oa-user-1",
        full_name="测试用户",
        department_name="主部门",
        department_code="primary",
        station_name="中级前端程序员",
        job_level_name="11",
        job_grade_name="基层",
        mobile_number="18670050179",
    )
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "enabled", True)
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "userinfo_url", "https://oa.example.test/userinfo")
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "company_code", "TEST")
    monkeypatch.setattr(oa_sso_service, "fetch_oa_identity", AsyncMock(return_value=identity))
    monkeypatch.setattr(oa_sso_service, "log_operation", AsyncMock())
    monkeypatch.setattr(oa_sso_service.AuthUtils, "hash_password", lambda _password: "hashed")
    monkeypatch.setattr(oa_sso_service.AuthUtils, "create_access_token", lambda data: f"yuxi-{data['sub']}")
    token = issue_oa_token()

    first = await oa_sso_service.exchange_oa_token_handler(token, oa_session)
    user = await oa_session.scalar(select(User).where(User.uid == "oa:TEST:oa-user-1"))
    # OA 返回的姓名、岗位与职级都落库：改成本地旧值后再登录应被刷新回来
    user.display_name = "旧姓名"
    user.oa_station_name = "旧岗位"
    user.oa_job_level_name = "旧职级"
    await oa_session.commit()
    second = await oa_sso_service.exchange_oa_token_handler(token, oa_session)

    assert first["access_token"] == second["access_token"]
    assert first["uid"] == "oa:TEST:oa-user-1"
    assert first["display_name"] == "测试用户"
    assert second["display_name"] == "测试用户"
    assert [role["code"] for role in first["roles"]] == ["user"]
    assert first["effective_permissions"] == ["agent:use"]
    assert first["phone_number"] is None
    assert first["department_name"] == "主部门"
    # OA 展示资料随登录响应透出，其中姓名、岗位与职级同时落库（手机号、头像仍由用户自行维护）
    assert first["oa_profile"]["station_name"] == "中级前端程序员"
    assert first["oa_profile"]["job_level_name"] == "11"
    assert first["oa_profile"]["mobile_number"] == "18670050179"
    assert first["oa_profile"]["company_full_name"] is None
    assert await oa_session.scalar(select(func.count(User.id))) == 1
    assert user.display_name == "测试用户"
    assert user.oa_station_name == "中级前端程序员"
    assert user.oa_job_level_name == "11（基层）"
    assert user.phone_number is None
    assert user.avatar is None


async def test_oa_account_exchange_creates_user_without_persisting_external_tokens(monkeypatch, oa_session):
    """账号换票只确认 OA 返回凭证，响应中不得包含外部 token。"""
    captured_request = {}
    response_payloads = [
        {
            "data": {
                "status": "1",
                "data": {"account": "oa-user-1", "oaToken": "oa-token", "saToken": "sa-token"},
            }
        },
        {"data": {"account": "oa-user-1", "oaToken": "oa-token", "saToken": "sa-token"}},
    ]

    class FakeAsyncClient:
        def __init__(self, **options):
            captured_request["options"] = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json, headers):
            captured_request.update({"url": url, "json": json, "headers": headers})
            return httpx.Response(200, json=response_payloads.pop(0))

    monkeypatch.setattr(oa_sso_service.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("YUXI_ENV", "production")
    monkeypatch.setattr(oa_sso_service.oa_account_login_config, "enabled", True)
    monkeypatch.setattr(oa_sso_service.oa_account_login_config, "login_url", "https://oa.example.test/login")
    monkeypatch.setattr(oa_sso_service.oa_account_login_config, "company_code", "TEST")
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "enabled", True)
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "userinfo_url", "https://oa.example.test/userinfo")
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "company_code", "TEST")
    fetch_identity = AsyncMock(
        return_value=oa_sso_service.OAIdentity(
            company_code="TEST",
            account="oa-user-1",
            full_name="测试用户",
            department_name="主部门",
        )
    )
    monkeypatch.setattr(oa_sso_service, "fetch_oa_identity", fetch_identity)
    monkeypatch.setattr(oa_sso_service, "log_operation", AsyncMock())
    monkeypatch.setattr(oa_sso_service.AuthUtils, "hash_password", lambda _password: "hashed")
    monkeypatch.setattr(oa_sso_service.AuthUtils, "create_access_token", lambda data: f"yuxi-{data['sub']}")

    response = await oa_sso_service.exchange_oa_account_handler(" oa-user-1 ", oa_session)
    repeated_response = await oa_sso_service.exchange_oa_account_handler("oa-user-1", oa_session)

    assert response["uid"] == "oa:TEST:oa-user-1"
    assert repeated_response["user_id"] == response["user_id"]
    assert response["department_id"] == 2
    assert "oaToken" not in response and "saToken" not in response
    assert fetch_identity.await_args_list[0].args == ("oa-token", "oa-user-1")
    assert captured_request == {
        "options": {"follow_redirects": False, "timeout": 10.0},
        "url": "https://oa.example.test/login",
        "json": {"account": "oa-user-1", "deviceId": "H5", "companyCode": "TEST", "loginType": "8"},
        "headers": {"Accept": "application/json"},
    }


async def test_oa_account_exchange_rejects_missing_external_tokens(monkeypatch, oa_session):
    """OA 未同时返回两个凭证时不得建立本地登录态。"""

    class FakeAsyncClient:
        def __init__(self, **_options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return httpx.Response(200, json={"data": {"oaToken": "oa-token"}})

    monkeypatch.setattr(oa_sso_service.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(oa_sso_service.oa_account_login_config, "enabled", True)
    monkeypatch.setattr(oa_sso_service.oa_account_login_config, "login_url", "https://oa.example.test/login")
    monkeypatch.setattr(oa_sso_service.oa_account_login_config, "company_code", "TEST")
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "enabled", True)
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "userinfo_url", "https://oa.example.test/userinfo")
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "company_code", "TEST")

    with pytest.raises(HTTPException, match="账号登录失败"):
        await oa_sso_service.exchange_oa_account_handler("oa-user-1", oa_session)


async def test_oa_account_exchange_rejects_returned_account_mismatch(monkeypatch, oa_session):
    """OA 返回的账号与请求账号不一致时不得建立本地登录态。"""

    class FakeAsyncClient:
        def __init__(self, **_options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "1",
                        "data": {"account": "another-user", "oaToken": "oa-token", "saToken": "sa-token"},
                    }
                },
            )

    monkeypatch.setattr(oa_sso_service.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(oa_sso_service.oa_account_login_config, "enabled", True)
    monkeypatch.setattr(oa_sso_service.oa_account_login_config, "login_url", "https://oa.example.test/login")
    monkeypatch.setattr(oa_sso_service.oa_account_login_config, "company_code", "TEST")
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "enabled", True)
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "userinfo_url", "https://oa.example.test/userinfo")
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "company_code", "TEST")

    with pytest.raises(HTTPException, match="账号校验失败"):
        await oa_sso_service.exchange_oa_account_handler("oa-user-1", oa_session)


async def test_oa_account_exchange_does_not_login_when_oa_identity_validation_fails(monkeypatch, oa_session):
    """父页面传入账号和换票结果都不能代替 OA 用户信息校验。"""

    class FakeAsyncClient:
        def __init__(self, **_options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return httpx.Response(
                200,
                json={"data": {"account": "oa-user-1", "oaToken": "oa-token", "saToken": "sa-token"}},
            )

    monkeypatch.setattr(oa_sso_service.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(oa_sso_service.oa_account_login_config, "enabled", True)
    monkeypatch.setattr(oa_sso_service.oa_account_login_config, "login_url", "https://oa.example.test/login")
    monkeypatch.setattr(oa_sso_service.oa_account_login_config, "company_code", "TEST")
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "enabled", True)
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "userinfo_url", "https://oa.example.test/userinfo")
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "company_code", "TEST")
    monkeypatch.setattr(
        oa_sso_service,
        "fetch_oa_identity",
        AsyncMock(side_effect=HTTPException(status.HTTP_401_UNAUTHORIZED, "OA 登录凭证已失效")),
    )

    with pytest.raises(HTTPException, match="凭证已失效"):
        await oa_sso_service.exchange_oa_account_handler("oa-user-1", oa_session)

    assert await oa_session.scalar(select(func.count(User.id))) == 0


def _local_account_user(**overrides) -> User:
    """构造本地账号（非 OA 身份）用于验证按工号反查补全。"""
    data = {
        "id": 7,
        "username": "2024102811",
        "uid": "u2024102811",
        "display_name": None,
        "oa_station_name": None,
        "oa_job_level_name": None,
    }
    data.update(overrides)
    return User(**data)


def _configure_oa(monkeypatch) -> None:
    """打开 OA 用户接口配置（单测不访问真实网络）。"""
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "enabled", True)
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "userinfo_url", "https://oa.example.test/userinfo")
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "company_code", "TEST")


def _oa_user_payload(**overrides) -> dict:
    """OA 用户信息接口的最小返回；账号固定为 2024102811。"""
    payload = {
        "account": "2024102811",
        "companyCode": "TEST",
        "fullName": "吴轩",
        "userStateCode": "service",
        "userJobInformationDtos": [
            {
                "pagingSort": 1,
                "appointmentDepartmentName": "研发部",
                "appointmentStationName": "中级前端程序员",
                "jobLevelName": "11",
                "jobGradeName": "基层",
            }
        ],
    }
    payload.update(overrides)
    return payload


class _OaInfoClient:
    """记录请求参数并按给定返回体应答的 httpx 替身。"""

    captured: dict = {}
    payload: dict = {}

    def __init__(self, **options):
        _OaInfoClient.captured["options"] = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, *, params, headers):
        _OaInfoClient.captured.update({"url": url, "params": params, "headers": headers})
        return httpx.Response(200, json={"status": 1, "data": _OaInfoClient.payload})


async def test_local_account_backfill_fills_display_name_and_station(monkeypatch):
    """本地账号按工号反查 OA，补齐展示姓名、岗位与职级，且不伪造 OA 凭证。"""
    _OaInfoClient.captured = {}
    _OaInfoClient.payload = _oa_user_payload()
    monkeypatch.setattr(oa_sso_service.httpx, "AsyncClient", _OaInfoClient)
    _configure_oa(monkeypatch)
    user = _local_account_user()

    assert await oa_sso_service.backfill_local_user_oa_profile(None, user) is True

    assert user.display_name == "吴轩"
    assert user.oa_station_name == "中级前端程序员"
    # 职级按「职级（职等）」展示文本落库
    assert user.oa_job_level_name == "11（基层）"
    assert _OaInfoClient.captured["params"] == {"Account": "2024102811"}
    # 反查链路没有 OA 凭证：不得带 Authorization 头，超时也须短于 SSO 的 10s
    assert _OaInfoClient.captured["headers"] == {"Accept": "application/json"}
    assert _OaInfoClient.captured["options"] == {"follow_redirects": False, "timeout": 3.0}


async def test_local_account_backfill_is_idempotent(monkeypatch):
    """重复反查同一份岗位资料不产生改动，避免无谓写库。"""
    _OaInfoClient.captured = {}
    _OaInfoClient.payload = _oa_user_payload()
    monkeypatch.setattr(oa_sso_service.httpx, "AsyncClient", _OaInfoClient)
    _configure_oa(monkeypatch)
    user = _local_account_user(
        display_name="吴轩",
        oa_station_name="中级前端程序员",
        oa_job_level_name="11（基层）",
    )

    assert await oa_sso_service.backfill_local_user_oa_profile(None, user) is False


@pytest.mark.parametrize(
    ("username", "uid"),
    [
        ("2024102811", "oa:TEST:2024102811"),  # 已是 OA 身份，资料由 SSO 链路维护
        ("local-admin", "local-admin"),  # 账号不是工号，不得拿用户名猜他人档案
        ("12345", None),  # 位数不足 6 位，同样不发起请求
        ("2024102811A", None),  # 含非数字字符，不是工号格式
    ],
)
async def test_local_account_backfill_skips_without_request(monkeypatch, username, uid):
    """OA 身份与非工号账号都不发起反查请求。"""
    _OaInfoClient.captured = {}
    _OaInfoClient.payload = _oa_user_payload()
    monkeypatch.setattr(oa_sso_service.httpx, "AsyncClient", _OaInfoClient)
    _configure_oa(monkeypatch)
    user = _local_account_user(username=username, uid=uid or username)

    assert await oa_sso_service.backfill_local_user_oa_profile(None, user) is False
    assert _OaInfoClient.captured == {}
    assert user.display_name is None
    assert user.oa_station_name is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"account": "another-account"},  # 返回账号与被查工号不一致
        {"companyCode": "OTHER"},  # 跨公司返回不得采纳
        {"userStateCode": "left"},  # 离职人员不补全资料
    ],
    ids=["account-mismatch", "company-mismatch", "resigned-employee"],
)
async def test_local_account_backfill_rejects_untrusted_response(monkeypatch, overrides):
    """OA 返回与工号、公司或在职状态不符时保持本地资料不变。"""
    _OaInfoClient.captured = {}
    _OaInfoClient.payload = _oa_user_payload(**overrides)
    monkeypatch.setattr(oa_sso_service.httpx, "AsyncClient", _OaInfoClient)
    _configure_oa(monkeypatch)
    user = _local_account_user()

    assert await oa_sso_service.backfill_local_user_oa_profile(None, user) is False
    assert user.display_name is None
    assert user.oa_station_name is None


async def test_local_account_backfill_survives_oa_failure(monkeypatch):
    """OA 不可用时反查失败只返回 False，不得中断登录链路。"""

    class _FailingClient:
        def __init__(self, **_options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            raise httpx.ConnectError("oa unreachable")

    monkeypatch.setattr(oa_sso_service.httpx, "AsyncClient", _FailingClient)
    _configure_oa(monkeypatch)
    user = _local_account_user()

    assert await oa_sso_service.backfill_local_user_oa_profile(None, user) is False
    assert user.display_name is None
    assert user.oa_station_name is None


async def test_oa_exchange_keeps_existing_station_when_oa_omits_it(monkeypatch, oa_session):
    """OA 未返回岗位与职级时保留既有值，避免把已同步的用户资料清空。"""
    identity = oa_sso_service.OAIdentity(
        company_code="TEST",
        account="oa-user-1",
        full_name="测试用户",
        department_name="主部门",
    )
    assert identity.station_name is None and identity.job_level_text is None  # 场景前提：本次 OA 响应没有岗位与职级
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "enabled", True)
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "userinfo_url", "https://oa.example.test/userinfo")
    monkeypatch.setattr(oa_sso_service.oa_sso_config, "company_code", "TEST")
    monkeypatch.setattr(oa_sso_service, "fetch_oa_identity", AsyncMock(return_value=identity))
    monkeypatch.setattr(oa_sso_service, "log_operation", AsyncMock())
    monkeypatch.setattr(oa_sso_service.AuthUtils, "create_access_token", lambda data: f"yuxi-{data['sub']}")

    oa_session.add(
        User(
            uid="oa:TEST:oa-user-1",
            username="测试用户",
            display_name="测试用户",
            oa_station_name="既有岗位",
            oa_job_level_name="既有职级",
            password_hash="hashed",
        )
    )
    await oa_session.commit()

    await oa_sso_service.exchange_oa_token_handler(issue_oa_token(), oa_session)

    user = await oa_session.scalar(select(User).where(User.uid == "oa:TEST:oa-user-1"))
    assert user is not None
    assert user.oa_station_name == "既有岗位"
    assert user.oa_job_level_name == "既有职级"


@pytest.mark.parametrize(
    ("level", "grade", "expected"),
    [
        ("11", "基层", "11（基层）"),
        ("11", None, "11"),
        (None, "基层", "基层"),
        (None, None, None),
    ],
)
async def test_compose_job_level_text(level, grade, expected):
    """职级展示文本：两项齐全用「职级（职等）」，缺一单独展示，都缺返回 None。"""
    assert oa_sso_service._compose_job_level(level, grade) == expected
