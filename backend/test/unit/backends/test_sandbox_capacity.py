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
