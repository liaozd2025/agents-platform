"""OA 自定义 SSO 与 OIDC 多副本认证的真实 HTTP 测试。"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from yuxi.storage.postgres.models_business import ROOT_DEPARTMENT_ID

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


async def test_oidc_callback_and_exchange_work_across_api_replicas():
    provider_port, api_a_port, api_b_port = (_unused_port() for _ in range(3))
    issuer = f"http://127.0.0.1:{provider_port}"
    api_a = f"http://127.0.0.1:{api_a_port}"
    api_b = f"http://127.0.0.1:{api_b_port}"
    env = {
        **os.environ,
        "YUXI_ENV": "development",
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
        assert oa_me_response.status_code == 200
        assert oa_me_response.json()["uid"] == "oa:TEST:oa-s0-user"
        assert exchange_response.status_code == 200
        assert exchange_response.json()["department_id"] == ROOT_DEPARTMENT_ID
        assert replay_response.status_code == 400
    finally:
        _stop_apps(processes)
