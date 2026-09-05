import asyncio
from types import SimpleNamespace

import httpx
import pytest

from yuxi.agents.backends.sandbox.provisioner_client import ProvisionerClient, SandboxCapacityError
from yuxi.services import chat_service


@pytest.mark.parametrize(
    ("status", "body", "expected_type"),
    [
        (503, '{"detail":{"code":"sandbox_capacity_exhausted","scope":"global","limit":6}}', SandboxCapacityError),
        (503, '{"detail":"quiescing"}', RuntimeError),
        (503, "<html>unavailable</html>", RuntimeError),
        (503, "[]", RuntimeError),
        (500, '{"detail":{"code":"sandbox_capacity_exhausted"}}', RuntimeError),
        (401, '{"detail":"invalid credentials"}', RuntimeError),
    ],
)
def test_provisioner_client_retries_only_structured_capacity_failure(monkeypatch, status, body, expected_type):
    client = ProvisionerClient("http://provisioner", token="test")
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: httpx.Response(status, text=body))

    with pytest.raises(expected_type) as error:
        client.create("sandbox", "thread", "user")

    assert type(error.value) is expected_type


@pytest.fixture
def sandbox_bootstrap(monkeypatch):
    """只替换外部文件与容器副作用，保留 async 等待与异常传播。"""
    monkeypatch.setattr(chat_service, "get_user_skills_root_dir", lambda _uid: None)
    state = SimpleNamespace(available=False, calls=0, error=SandboxCapacityError({"scope": "global"}))

    def ensure_available():
        state.calls += 1
        if not state.available:
            raise state.error

    monkeypatch.setattr(
        chat_service, "ProvisionerSandboxBackend", lambda **_kwargs: SimpleNamespace(ensure_available=ensure_available)
    )
    return state


async def test_sandbox_bootstrap_waits_for_capacity_without_blocking_event_loop(monkeypatch, sandbox_bootstrap):
    waits = []

    async def release_capacity(delay):
        waits.append(delay)
        sandbox_bootstrap.available = True

    monkeypatch.setattr(chat_service.asyncio, "sleep", release_capacity)

    await chat_service._ensure_persistent_sandbox(runtime_scope_id="root", uid="user", workdir_path="project")

    assert sandbox_bootstrap.available
    assert sandbox_bootstrap.calls == 2
    assert waits == [5]


async def test_sandbox_capacity_wait_can_be_cancelled(monkeypatch, sandbox_bootstrap):
    waiting = asyncio.Event()

    async def wait_for_capacity(_delay):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(chat_service.asyncio, "sleep", wait_for_capacity)
    task = asyncio.create_task(
        chat_service._ensure_persistent_sandbox(runtime_scope_id="root", uid="user", workdir_path="project")
    )
    await asyncio.wait_for(waiting.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert sandbox_bootstrap.calls == 1


async def test_sandbox_capacity_wait_has_explicit_timeout(monkeypatch, sandbox_bootstrap):
    monkeypatch.setenv("SANDBOX_CAPACITY_WAIT_SECONDS", "0")

    with pytest.raises(chat_service.SandboxCapacityTimeoutError, match="sandbox_capacity_timeout") as error:
        await chat_service._ensure_persistent_sandbox(runtime_scope_id="root", uid="user", workdir_path="project")

    assert error.value.code == "sandbox_capacity_timeout"
    assert sandbox_bootstrap.calls == 1


async def test_sandbox_bootstrap_does_not_retry_other_failures(sandbox_bootstrap):
    sandbox_bootstrap.error = RuntimeError("sandbox_resource_policy_mismatch")

    with pytest.raises(RuntimeError, match="sandbox_resource_policy_mismatch"):
        await chat_service._ensure_persistent_sandbox(runtime_scope_id="root", uid="user", workdir_path="project")

    assert sandbox_bootstrap.calls == 1


async def test_sandbox_capacity_wait_rejects_negative_timeout(monkeypatch, sandbox_bootstrap):
    monkeypatch.setenv("SANDBOX_CAPACITY_WAIT_SECONDS", "-1")

    with pytest.raises(ValueError, match="must not be negative"):
        await chat_service._ensure_persistent_sandbox(runtime_scope_id="root", uid="user", workdir_path="project")

    assert sandbox_bootstrap.calls == 0
