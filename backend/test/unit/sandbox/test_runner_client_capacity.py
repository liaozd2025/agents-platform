"""运行客户端必须将真实 runner 容量响应与其他故障区分。"""

import pytest

from yuxi.modules.agents.runtime.sandbox.container import SandboxRunnerClient
from yuxi.shared.errors import StateConflictError, UpstreamError


@pytest.mark.parametrize(
    "status, body, error_type",
    [
        (429, '{"detail":"容器并发已达到用户上限"}', StateConflictError),
        (503, '{"detail":"unavailable"}', UpstreamError),
        (503, "<html>unavailable</html>", UpstreamError),
        (503, "[]", UpstreamError),
        (500, '{"detail":"sandbox_capacity"}', UpstreamError),
        (401, '{"detail":"unauthorized"}', UpstreamError),
    ],
)
def test_runner_capacity_preserves_error_classification(httpx_mock, status, body, error_type):
    """容量只由协议状态确认；错误文本不得伪造可重试容量失败。"""
    httpx_mock.add_response(status_code=status, text=body)
    client = SandboxRunnerClient(base_url="https://runner.example", token="synthetic-test-token")
    with pytest.raises(error_type) as error:
        client.execute(
            uid="one",
            run_id="run-1",
            owner="owner-1",
            command_id="command-1",
            command="true",
            cwd="/home/user/workspace/projects/demo",
            workdir="projects/demo",
            timeout_seconds=1,
            output_budget=1024,
            stdin_data=None,
            python=False,
            envs={},
        )
    assert type(error.value) is error_type
    if status == 429:
        assert error.value.code == "sandbox_capacity"
    assert len(httpx_mock.get_requests()) == 1
