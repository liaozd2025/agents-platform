"""OIDC 临时凭据在独立 API 进程间共享的一次性语义测试。"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest
from yuxi.services.oidc_service import OIDCUtils

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_oidc_state_and_login_code_are_consumed_by_another_process():
    """进程 A 写入 Redis 后，进程 B 能消费且进程 A 无法重放。"""
    state = await OIDCUtils.generate_state("/embed/thread-1", "nonce-1")
    login_code = await OIDCUtils.generate_login_code({"access_token": "replica-token", "user_id": 7})
    env = {
        **os.environ,
        "OIDC_TEST_STATE": state,
        "OIDC_TEST_LOGIN_CODE": login_code,
    }
    script = textwrap.dedent(
        """
        import asyncio
        import os

        from yuxi.services.oidc_service import OIDCUtils

        async def consume():
            state_data = await OIDCUtils.verify_state(os.environ["OIDC_TEST_STATE"])
            login_data = await OIDCUtils.consume_login_code(os.environ["OIDC_TEST_LOGIN_CODE"])
            assert state_data == {"redirect_path": "/embed/thread-1", "nonce": "nonce-1"}
            assert login_data == {"access_token": "replica-token", "user_id": 7}

        asyncio.run(consume())
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert await OIDCUtils.verify_state(state) is None
    assert await OIDCUtils.consume_login_code(login_code) is None
