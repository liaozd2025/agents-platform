"""OA 自定义 SSO 与 OIDC 多副本认证的真实 HTTP 测试。"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.infrastructure.postgres.base import Base
from yuxi.modules.identity.models import GROUP_NODE_TYPE, ROOT_DEPARTMENT_ID, Department, Role, User
from yuxi.modules.identity.repositories.users import UserRepository
from yuxi.modules.identity.security import AuthUtils
from yuxi.modules.system.models import OperationLog

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


@pytest_asyncio.fixture(autouse=True)
async def isolated_schema(monkeypatch):
    """每例建立独立角色/组织 Schema，各测试进程不接触共享业务表。"""
    schema = "pytest_oidc_replica_" + uuid.uuid4().hex
    admin = create_async_engine(os.environ["POSTGRES_URL"])
    engine = create_async_engine(os.environ["POSTGRES_URL"], connect_args={"server_settings": {"search_path": schema}})
    monkeypatch.setenv("YUXI_TEST_SCHEMA", schema)
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            db.add_all(
                [
                    Department(name="集团", node_type=GROUP_NODE_TYPE, path=f"/{ROOT_DEPARTMENT_ID}/"),
                    Role(code="user", name="普通用户", is_builtin=True, default_scope_type="self"),
                ]
            )
            await db.commit()
        yield
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


def _unused_port() -> int:
    """由操作系统分配一个本地端口。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_app(module: str, port: int, env: dict[str, str]) -> subprocess.Popen:
    """启动一个独立 HTTP 测试进程。"""
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _wait_until_ready(url: str, process: subprocess.Popen) -> None:
    """等待 HTTP 进程就绪，或在进程提前退出时失败。"""
    async with httpx.AsyncClient(timeout=1) as client:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail(f"OIDC 测试进程提前退出: {process.returncode}")
            try:
                if (await client.get(url)).status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    pytest.fail(f"OIDC 测试进程未就绪: {url}")


def _stop_apps(processes: list[subprocess.Popen]) -> None:
    """停止本测试启动的局部进程。"""
    for process in reversed(processes):
        process.terminate()
    for process in reversed(processes):
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("mode", ["local-success", "local-failure", "oa-token", "oa-account"])
async def test_oa_profile_survives_real_http_and_is_committed(mode):
    """真实认证进程访问受控 OA HTTP 服务，响应字段与数据库提交必须一致。"""
    account = f"2024{int(uuid.uuid4().hex[:8], 16) % 10**8:08d}"
    local = mode.startswith("local-")
    uid = f"pytest-local-{account}" if local else f"oa:TEST:{account}"
    password = f"Pw!{uuid.uuid4().hex}"
    token = jwt.encode({"data": {"account": account}}, "test-oa-profile-token-secret-32-bytes", algorithm="HS256")
    calls = []
    profile = {
        "account": account,
        "companyCode": "TEST",
        "userStateCode": "service",
        "fullName": "资料测试姓名",
        "userJobInformationDtos": [
            {
                "pagingSort": 1,
                "appointmentStationName": "资料测试岗位",
                "jobLevelName": "11",
                "jobGradeName": "基层",
            }
        ],
    }

    class Provider(BaseHTTPRequestHandler):
        """提供当前用例的 OA 返回值与可观察故障。"""

        def respond(self, status_code, data):
            """返回 JSON，供独立 API 进程通过网络读取。"""
            body = json.dumps(data).encode()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            """记录反查账号并返回资料或上游错误。"""
            calls.append(parse_qs(urlsplit(self.path).query).get("Account"))
            self.respond(503 if mode == "local-failure" else 200, {"status": 1, "data": profile})

        def do_POST(self):
            """模拟账号换取 OA 凭证。"""
            self.rfile.read(int(self.headers["Content-Length"]))
            self.respond(200, {"data": {"account": account, "oaToken": token, "saToken": token}})

        def log_message(self, *args):
            """测试不输出请求信息。"""

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    provider_url = f"http://127.0.0.1:{provider.server_port}"
    port = _unused_port()
    api_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "YUXI_ENV": "development",
        "YUXI_INSTANCE_ID": "pytest-oidc-replicas",
        "JWT_SECRET_KEY": "pytest-oidc-replica-shared-jwt-secret",
        "OA_SSO_ENABLED": "true",
        "OA_SSO_USERINFO_URL": f"{provider_url}/userinfo",
        "OA_SSO_COMPANY_CODE": "TEST",
        "OA_ACCOUNT_LOGIN_ENABLED": "true",
        "OA_ACCOUNT_LOGIN_URL": f"{provider_url}/login",
        "OA_ACCOUNT_LOGIN_COMPANY_CODE": "TEST",
    }
    engine = create_async_engine(
        os.environ["POSTGRES_URL"], connect_args={"server_settings": {"search_path": os.environ["YUXI_TEST_SCHEMA"]}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    processes = []
    try:
        if local:
            async with sessions() as db:
                await UserRepository(db).create_with_db(
                    db,
                    {
                        "username": account,
                        "uid": uid,
                        "password_hash": AuthUtils.hash_password(password),
                        "department_id": ROOT_DEPARTMENT_ID,
                    },
                )
                await db.commit()
        processes.append(_start_app("test.integration.fixtures.oa_auth_replica:app", port, env))
        await _wait_until_ready(f"{api_url}/api/auth/oidc/config", processes[0])
        async with httpx.AsyncClient(base_url=api_url, timeout=10) as client:
            if local:
                response = await client.post("/api/auth/token", data={"username": account, "password": password})
            elif mode == "oa-token":
                response = await client.post("/api/auth/oa/exchange-token", json={"token": f"{token}|{token}"})
            else:
                response = await client.post("/api/auth/oa/exchange-account", json={"account": account})
        assert response.status_code == 200, response.text
        assert response.json()["access_token"]
        assert calls == [[account]]
        async with sessions() as db:
            stored = (await db.scalars(select(User).where(User.uid == uid))).one()
            operation = await db.scalar(select(OperationLog).where(OperationLog.user_id == stored.id).order_by(OperationLog.id.desc()))
            assert operation is not None and operation.ip_address == "127.0.0.1"
            expected = (None, None, None) if mode == "local-failure" else ("资料测试姓名", "资料测试岗位", "11（基层）")
            assert (stored.display_name, stored.oa_station_name, stored.oa_job_level_name) == expected
            if local:
                assert stored.username == account
            else:
                assert response.json()["oa_profile"]["station_name"] == "资料测试岗位"
                assert response.json()["oa_profile"]["job_level_name"] == "11"
    finally:
        _stop_apps(processes)
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
        async with sessions() as db:
            user_ids = select(User.id).where(User.uid == uid)
            await db.execute(delete(OperationLog).where(OperationLog.user_id.in_(user_ids)))
            await db.execute(delete(User).where(User.uid == uid))
            await db.commit()
        await engine.dispose()


async def test_oidc_callback_and_exchange_work_across_api_replicas():
    provider_port, api_a_port, api_b_port = (_unused_port() for _ in range(3))
    issuer = f"http://127.0.0.1:{provider_port}"
    api_a = f"http://127.0.0.1:{api_a_port}"
    api_b = f"http://127.0.0.1:{api_b_port}"
    env = {
        **os.environ,
        "YUXI_ENV": "development",
        "YUXI_INSTANCE_ID": "pytest-oidc-replicas",
        "JWT_SECRET_KEY": "pytest-oidc-replica-shared-jwt-secret",
        "OIDC_ENABLED": "true",
        "OIDC_ISSUER_URL": issuer,
        "OIDC_CLIENT_ID": "oa-s0-local-client",
        "OIDC_CLIENT_SECRET": "oa-s0-local-secret",
        "OIDC_REDIRECT_URI": f"{api_a}/api/auth/oidc/callback",
        "OIDC_DEPARTMENT_CLAIM": "department",
        "OA_SSO_ENABLED": "true",
        "OA_SSO_USERINFO_URL": f"{issuer}/oa-api/User/GetUserInfo",
        "OA_SSO_COMPANY_CODE": "TEST",
        "YUXI_EMBED_ALLOWED_ORIGINS": "http://localhost:4173",
        "MOCK_OIDC_ISSUER": issuer,
        "MOCK_OIDC_BROWSER_ORIGIN": issuer,
    }
    processes = [
        _start_app("test.e2e.fixtures.oa_oidc_mock:app", provider_port, env),
        _start_app("test.integration.fixtures.oa_auth_replica:app", api_a_port, env),
        _start_app("test.integration.fixtures.oa_auth_replica:app", api_b_port, env),
    ]

    try:
        await asyncio.gather(
            _wait_until_ready(f"{issuer}/.well-known/openid-configuration", processes[0]),
            _wait_until_ready(f"{api_a}/api/auth/oidc/config", processes[1]),
            _wait_until_ready(f"{api_b}/api/auth/oidc/config", processes[2]),
        )
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            mock_token_response = await client.get(f"{issuer}/mock-oa-token")
            oa_exchange_response = await client.post(
                f"{api_a}/api/auth/oa/exchange-token",
                json={"token": mock_token_response.json()["token"]},
            )
            assert oa_exchange_response.status_code == 200
            oa_login = oa_exchange_response.json()
            oa_me_response = await client.get(
                f"{api_b}/api/auth/me",
                headers={"Authorization": f"Bearer {oa_login['access_token']}"},
            )

            login_response = await client.get(
                f"{api_a}/api/auth/oidc/login-url",
                params={"redirect_path": "http://localhost:4173/oa/callback"},
            )
            login_response.raise_for_status()

            authorize_response = await client.get(login_response.json()["login_url"])
            callback_response = await client.get(authorize_response.headers["location"])
            assert callback_response.status_code == 302

            callback_query = parse_qs(urlsplit(callback_response.headers["location"]).query)
            exchange_code = callback_query["code"][0]
            exchange_response = await client.post(
                f"{api_b}/api/auth/oidc/exchange-code",
                json={"code": exchange_code},
            )
            replay_response = await client.post(
                f"{api_a}/api/auth/oidc/exchange-code",
                json={"code": exchange_code},
            )

        assert oa_login["uid"] == "oa:TEST:oa-s0-user"
        assert oa_me_response.status_code == 200, oa_me_response.text
        assert oa_me_response.json()["uid"] == "oa:TEST:oa-s0-user"
        assert exchange_response.status_code == 200
        assert exchange_response.json()["department_id"] == ROOT_DEPARTMENT_ID
        assert replay_response.status_code == 200
        assert replay_response.json() == exchange_response.json()
    finally:
        _stop_apps(processes)


@pytest.mark.parametrize("configured,status_code,message", [(False, 503, "OA 免登录未配置"), (True, 401, "OA 登录凭证格式无效")])
async def test_oa_domain_rejections_keep_http_status_and_message(configured, status_code, message):
    """真实API进程通过生产handler映射领域身份拒绝与能力未配置，不改公开错误文案。"""
    port = _unused_port()
    env = {
        **os.environ,
        "OIDC_ENABLED": "false",
        "OA_SSO_ENABLED": "true" if configured else "false",
        "OA_SSO_USERINFO_URL": "https://oa.example.test/userinfo",
        "OA_SSO_COMPANY_CODE": "TEST",
    }
    process = _start_app("test.integration.fixtures.oa_auth_replica:app", port, env)
    try:
        await _wait_until_ready(f"http://127.0.0.1:{port}/api/auth/oidc/config", process)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            response = await client.post("/api/auth/oa/exchange-token", json={"token": "not-a-jwt"})
            assert response.status_code == status_code, response.text
            assert response.json() == {"detail": message}
    finally:
        _stop_apps([process])


@pytest.mark.parametrize("existing", [False, True])
async def test_oa_audit_failure_rolls_back_user_and_profile(monkeypatch, existing):
    """强制审计失败不能留下新账户或已更新资料，服务不发布未审计的登录态。"""
    from yuxi.modules.identity.services import oa
    from yuxi.shared.errors import OperationFailedError

    engine = create_async_engine(
        os.environ["POSTGRES_URL"], connect_args={"server_settings": {"search_path": os.environ["YUXI_TEST_SCHEMA"]}}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    identity = oa.OAIdentity(company_code="TEST", account="fixture", full_name="新资料", station_name="新岗位")
    original_audit = oa.log_operation

    async def fail_audit(*args, **kwargs):
        """先执行真实flush，再模拟审计发布故障验证整体回滚。"""
        await original_audit(*args, **kwargs)
        raise OperationFailedError("fixture audit failed")

    monkeypatch.setattr(oa, "log_operation", fail_audit)
    try:
        if existing:
            async with factory() as db:
                await UserRepository(db).create_with_db(
                    db,
                    {
                        "uid": identity.uid,
                        "username": "original",
                        "display_name": "原资料",
                        "password_hash": "!fixture",
                        "department_id": ROOT_DEPARTMENT_ID,
                    },
                )
                await db.commit()
        async with factory() as db:
            department = await db.get(Department, ROOT_DEPARTMENT_ID)
            with pytest.raises(OperationFailedError, match="fixture audit failed"):
                await oa._complete_oa_login(identity, db, "127.0.0.1", department)
        async with factory() as db:
            user = await db.scalar(select(User).where(User.uid == identity.uid))
            if existing:
                assert user is not None and user.display_name == "原资料" and user.oa_station_name is None
            else:
                assert user is None
            assert await db.scalar(select(func.count()).select_from(OperationLog)) == 0
    finally:
        await engine.dispose()
