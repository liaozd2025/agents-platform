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
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from yuxi.storage.postgres.models_business import ROOT_DEPARTMENT_ID, OperationLog, User
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


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
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    processes = []
    try:
        if local:
            async with sessions() as db:
                db.add(
                    User(
                        username=account,
                        uid=uid,
                        password_hash=AuthUtils.hash_password(password),
                        department_id=ROOT_DEPARTMENT_ID,
                    )
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
        assert replay_response.status_code == 400
    finally:
        _stop_apps(processes)
