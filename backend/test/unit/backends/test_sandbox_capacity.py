import asyncio
from types import SimpleNamespace

import httpx
import pytest

from yuxi.agents.backends.sandbox.provisioner_client import ProvisionerClient, SandboxCapacityError
from yuxi.agents.backends.sandbox import backend as sandbox_backend


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
    state = SimpleNamespace(available=False, calls=0, error=SandboxCapacityError({"scope": "global"}))

    def ensure_available(self):
        state.calls += 1
        if not state.available:
            raise state.error

    monkeypatch.setattr(sandbox_backend.ProvisionerSandboxBackend, "ensure_available", ensure_available)
    return state


@pytest.mark.parametrize("operation", ["execute", "upload", "edit", "write"])
def test_file_operation_preserves_capacity_failure(monkeypatch, operation):
    """容量不足不能被报告为权限错误或文件不存在，也不能尝试写文件。"""

    def exhausted(*_args, **_kwargs):
        raise SandboxCapacityError({"scope": "global"})

    monkeypatch.setattr(sandbox_backend, "get_sandbox_provider", lambda: SimpleNamespace(get=exhausted))
    backend = sandbox_backend.ProvisionerSandboxBackend(thread_id="root", uid="user")
    path = "/home/gem/user-data/report.txt"
    with pytest.raises(SandboxCapacityError, match="sandbox_capacity_exhausted"):
        if operation == "execute":
            backend.execute("echo ready")
        elif operation == "upload":
            backend.upload_files([(path, b"content")])
        elif operation == "edit":
            backend.edit(path, "old", "new")
        else:
            backend.write(path, "content")
    assert backend._client is None


async def test_sandbox_bootstrap_waits_for_capacity_without_blocking_event_loop(monkeypatch, sandbox_bootstrap):
    waits = []

    async def release_capacity(delay):
        waits.append(delay)
        sandbox_bootstrap.available = True

    monkeypatch.setattr(sandbox_backend.asyncio, "sleep", release_capacity)

    await sandbox_backend.ProvisionerSandboxBackend(
        thread_id="root", uid="user", workdir_path="project"
    ).aensure_available()

    assert sandbox_bootstrap.available
    assert sandbox_bootstrap.calls == 2
    assert waits == [5]


async def test_sandbox_capacity_wait_can_be_cancelled(monkeypatch, sandbox_bootstrap):
    waiting = asyncio.Event()

    async def wait_for_capacity(_delay):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(sandbox_backend.asyncio, "sleep", wait_for_capacity)
    task = asyncio.create_task(
        sandbox_backend.ProvisionerSandboxBackend(
            thread_id="root", uid="user", workdir_path="project"
        ).aensure_available()
    )
    await asyncio.wait_for(waiting.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert sandbox_bootstrap.calls == 1


async def test_sandbox_capacity_wait_has_explicit_timeout(monkeypatch, sandbox_bootstrap):
    monkeypatch.setenv("SANDBOX_CAPACITY_WAIT_SECONDS", "0")

    with pytest.raises(sandbox_backend.SandboxCapacityTimeoutError, match="sandbox_capacity_timeout") as error:
        await sandbox_backend.ProvisionerSandboxBackend(
            thread_id="root", uid="user", workdir_path="project"
        ).aensure_available()

    assert error.value.code == "sandbox_capacity_timeout"
    assert sandbox_bootstrap.calls == 1


async def test_sandbox_bootstrap_does_not_retry_other_failures(sandbox_bootstrap):
    sandbox_bootstrap.error = RuntimeError("sandbox_resource_policy_mismatch")

    with pytest.raises(RuntimeError, match="sandbox_resource_policy_mismatch"):
        await sandbox_backend.ProvisionerSandboxBackend(
            thread_id="root", uid="user", workdir_path="project"
        ).aensure_available()

    assert sandbox_bootstrap.calls == 1


async def test_sandbox_capacity_wait_rejects_negative_timeout(monkeypatch, sandbox_bootstrap):
    monkeypatch.setenv("SANDBOX_CAPACITY_WAIT_SECONDS", "-1")

    with pytest.raises(ValueError, match="must not be negative"):
        await sandbox_backend.ProvisionerSandboxBackend(
            thread_id="root", uid="user", workdir_path="project"
        ).aensure_available()

    assert sandbox_bootstrap.calls == 0
