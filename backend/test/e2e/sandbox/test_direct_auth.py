"""通过真实 provisioner 和两个 Docker 沙箱验证直连执行认证。"""

import json
import os
import shlex
import uuid

import httpx
import pytest

pytestmark = pytest.mark.e2e


def test_cross_user_sandbox_requests_cannot_execute_or_download():
    """无凭据和持有 A 凭据时都不能操作 B，合法代理能读写同一文件。"""
    base = os.environ.get("TEST_SANDBOX_PROVISIONER_URL")
    prefix = os.environ.get("TEST_SANDBOX_PREFIX")
    if not base or not prefix:
        pytest.skip("需要隔离 Docker E2E 环境：TEST_SANDBOX_PROVISIONER_URL 和 TEST_SANDBOX_PREFIX")
    import docker

    headers = {"Authorization": "Bearer " + os.environ["SANDBOX_PROVISIONER_TOKEN"]}
    ids = [f"auth-{uuid.uuid4().hex[:12]}" for _ in range(2)]
    engine = docker.from_env()
    with httpx.Client(base_url=base, headers=headers, timeout=120, trust_env=False) as client:
        try:
            for sid in ids:
                response = client.post(
                    "/api/sandboxes",
                    json={
                        "sandbox_id": sid,
                        "thread_id": sid,
                        "uid": sid,
                        "inherit_env": False,
                    },
                )
                assert response.status_code == 200, response.text
                generation = response.json()["generation"]
                reused = client.post(
                    "/api/sandboxes", json={"sandbox_id": sid, "thread_id": sid, "uid": sid, "inherit_env": False}
                )
                assert reused.status_code == 200 and reused.json()["generation"] == generation, reused.text
            containers = [engine.containers.get(f"{prefix}-{sid}") for sid in ids]
            networks = [c.attrs["NetworkSettings"]["Networks"] for c in containers]
            assert not set(networks[0]) & set(networks[1])
            assert containers[0].labels["uid"] != containers[1].labels["uid"]
            target = "http://" + next(iter(networks[1].values()))["IPAddress"] + ":8080"
            marker = f"AUTH_BOUNDARY_{uuid.uuid4().hex}"
            path = "/home/gem/auth-boundary.txt"
            proxy = f"/api/sandboxes/{ids[1]}/proxy"
            response = client.post(
                proxy + "/v1/shell/exec",
                json={
                    "command": f"printf %s {shlex.quote(marker)} > {path}",
                    "timeout": 5,
                },
            )
            assert response.status_code == 200, response.text
            response = client.get(proxy + "/v1/file/download", params={"path": path})
            assert response.status_code == 200 and response.text == marker

            # 在 A 的执行环境中访问 B；只输出状态，不输出任一密钥。
            probe = """
import json, os, urllib.request, urllib.error
target, path = TARGET, PATH
results = []
for own_key in (False, True):
    headers = {'Content-Type': 'application/json'}
    if own_key: headers['X-AIO-API-Key'] = os.environ.get('SANDBOX_API_KEY', 'wrong-key')
    cases = [('/v1/shell/exec', json.dumps({'command': 'printf compromised > '+path, 'timeout': 5}).encode()),
             ('/v1/file/download?path='+path+'&fake=.json', None)]
    for endpoint, body in cases:
        try:
            req = urllib.request.Request(target+endpoint, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as r:
                results.append(r.status)
        except urllib.error.HTTPError as e: results.append(e.code)
print(json.dumps({"statuses": results, "own_key_present": bool(os.environ.get("SANDBOX_API_KEY"))}))
""".replace("TARGET", repr(target)).replace("PATH", repr(path))
            response = client.post(
                f"/api/sandboxes/{ids[0]}/proxy/v1/shell/exec",
                json={
                    "command": "python3 -c " + shlex.quote(probe),
                    "timeout": 15,
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["data"]["exit_code"] == 0, payload
            observed = json.loads(payload["data"]["output"].strip())
            statuses = observed["statuses"]
            response = client.get(proxy + "/v1/file/download", params={"path": path})
            assert response.status_code == 200 and response.text == marker, (statuses, response.text)
            assert statuses == [401, 401, 401, 401], statuses
            assert observed["own_key_present"], "A 内未读到自己的执行密钥，未覆盖持有效凭据的攻击路径"
        finally:
            for sid in ids:
                response = client.delete(f"/api/sandboxes/{sid}")
                assert response.status_code in (200, 404), response.text
            engine.close()
