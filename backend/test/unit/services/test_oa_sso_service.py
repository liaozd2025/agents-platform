"""OA 自定义 token 交换的身份边界测试。"""

from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
import pytest_asyncio
from fastapi import HTTPException
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
    second = await oa_sso_service.exchange_oa_token_handler(token, oa_session)

    assert first["access_token"] == second["access_token"]
    assert first["uid"] == "oa:TEST:oa-user-1"
    assert [role["code"] for role in first["roles"]] == ["user"]
    assert first["effective_permissions"] == ["agent:use"]
    assert first["phone_number"] is None
    assert first["department_name"] == "主部门"
    assert await oa_session.scalar(select(func.count(User.id))) == 1
