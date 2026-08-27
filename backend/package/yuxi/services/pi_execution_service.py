"""PI 运行时清单与执行 seam。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from yuxi.agents.backends.sandbox import (
    ProvisionerSandboxBackend,
    sandbox_outputs_dir,
    sandbox_user_data_dir,
)
from yuxi.agents.backends.sandbox.provider import get_sandbox_provider
from yuxi.agents.skills.service import compute_skill_dir_hash, sync_thread_readable_skills
from yuxi.services.agent_run_manifest_service import canonical_json, compute_manifest_fingerprint

PI_RUNNER_PROTOCOL = "yuxi.pi-jsonl.v1"
PI_PACKAGE_NAME = "@earendil-works/pi-coding-agent"
PI_PACKAGE_VERSION = "0.84.2"
PI_PACKAGE_INTEGRITY = "sha512-l4E+B7hgXKWddRo8bC/eSue2aWZjEgJ9xIpf5p0Og+lq8a2TArCwJ0HCoCPCgaBP/tN4zbYH/wOwvx9pJpeLCA=="
PI_NODE_VERSION = "22.21.0"
PI_GOLDEN_SKILL_VERSION = "1.0.0"
PI_RUNNER_SANDBOX_PATH = "/opt/yuxi-pi-runner/runner.mjs"
PI_SKILL_SANDBOX_PATH = "/home/gem/skills/pi-golden"
PI_RUNNER_DIR = Path(__file__).resolve().parents[1] / "pi_runner"
PI_RUNNER_PATH = PI_RUNNER_DIR / "runner.mjs"
PI_GOLDEN_SKILL_DIR = PI_RUNNER_DIR / "pi-golden"


class PiRuntimeMismatch(RuntimeError):
    """沙盒实际运行时与 attempt 锁定清单不一致。"""


class PiExecutionCancelled(RuntimeError):
    """PI attempt 在执行期间收到取消事实。"""


class PiExecutionUnknown(RuntimeError):
    """PI 已启动，但调用方无法证明其执行或结果提交结局。"""


class PiCleanupFailed(RuntimeError):
    """PI 实例删除失败，attempt 需要保留 orphan 事实。"""

    def __init__(self, message: str, *, primary: BaseException | None = None):
        super().__init__(message)
        self.primary = primary


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_pi_runtime_manifest(*, runner_path: Path, skill_dir: Path) -> tuple[dict, str]:
    """为首期 Local golden Task 构建不可变 Runtime Manifest。"""

    if not runner_path.is_file():
        raise ValueError(f"PI runner 不存在: {runner_path}")
    if not skill_dir.is_dir():
        raise ValueError(f"PI Skill 不存在: {skill_dir}")
    skill_digest = compute_skill_dir_hash(skill_dir)
    skill_item = {
        "slug": "pi-golden",
        "version": PI_GOLDEN_SKILL_VERSION,
        "path": PI_SKILL_SANDBOX_PATH,
        "digest": skill_digest,
    }
    manifest = {
        "manifest_version": 1,
        "runner": {
            "protocol": PI_RUNNER_PROTOCOL,
            "path": PI_RUNNER_SANDBOX_PATH,
            "digest": _file_digest(runner_path),
        },
        "node": {"version": PI_NODE_VERSION},
        "pi": {
            "package": PI_PACKAGE_NAME,
            "version": PI_PACKAGE_VERSION,
            "integrity": PI_PACKAGE_INTEGRITY,
        },
        "skill_bundle": {
            "id": "pi-golden",
            "version": PI_GOLDEN_SKILL_VERSION,
            "digest": hashlib.sha256(canonical_json([skill_item]).encode()).hexdigest(),
            "items": [skill_item],
            "read_only": True,
        },
        "policy": {"timeout_seconds": 60, "tools": ["read", "write"]},
    }
    return manifest, compute_manifest_fingerprint(manifest)


def build_default_pi_runtime_manifest() -> tuple[dict, str]:
    """从随服务发布的审核资产构建 Local PI Runtime Manifest。"""

    return build_pi_runtime_manifest(runner_path=PI_RUNNER_PATH, skill_dir=PI_GOLDEN_SKILL_DIR)


def validate_pi_runtime(manifest: dict, actual: dict) -> None:
    """在 PI 启动前逐项比对 Runner、Node、PI 与 Skill 摘要。"""

    expected = {
        "runner_protocol": manifest["runner"]["protocol"],
        "runner_digest": manifest["runner"]["digest"],
        "pi_version": manifest["pi"]["version"],
        "pi_integrity": manifest["pi"]["integrity"],
        "node_version": manifest["node"]["version"],
        "skills": {item["slug"]: item["digest"] for item in manifest["skill_bundle"]["items"]},
        "skill_bundle": manifest["skill_bundle"],
    }
    for field, value in expected.items():
        if actual.get(field) != value:
            raise PiRuntimeMismatch(f"runtime_mismatch: {field}")


async def _call_sink(result_sink, envelope: dict) -> Any:
    result = result_sink(envelope)
    return await result if inspect.isawaitable(result) else result


def build_pi_envelope(*, attempt: dict, adapter_name: str, event: dict) -> dict:
    """把 Runner 事件包装成可幂等持久化的统一 envelope。"""

    event_id = str(event.get("event_id") or "").strip()
    event_type = str(event.get("type") or "").strip()
    sequence = event.get("sequence")
    if not event_id or not event_type or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("PI event 缺少 event_id、type 或有效 sequence")
    value_key = "payload" if "payload" in event else "ref" if "ref" in event else None
    if value_key is None or not isinstance(event[value_key], dict):
        raise ValueError("PI event 必须包含对象 payload 或 ref")
    envelope = {
        "job_id": str(attempt["run_id"]),
        "attempt_id": str(attempt["attempt_id"]),
        "adapter": adapter_name,
        "event_id": event_id,
        "sequence": sequence,
        "type": event_type,
        "runtime_manifest_digest": attempt["manifest_digest"],
        value_key: event[value_key],
    }
    envelope[f"{value_key}_digest"] = hashlib.sha256(canonical_json(event[value_key]).encode()).hexdigest()
    return envelope


async def _execute_until_cancelled(adapter, instance_id: str, attempt: dict, cancel_event: asyncio.Event | None):
    if cancel_event is None:
        return await adapter.execute(instance_id, attempt)
    execute_task = asyncio.create_task(adapter.execute(instance_id, attempt))
    cancel_task = asyncio.create_task(cancel_event.wait())
    done, _ = await asyncio.wait({execute_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
    if cancel_task in done:
        execute_task.cancel()
        await asyncio.gather(execute_task, return_exceptions=True)
        raise PiExecutionCancelled("PI attempt cancelled")
    cancel_task.cancel()
    await asyncio.gather(cancel_task, return_exceptions=True)
    return execute_task.result()


async def execute_pi_attempt(
    *, attempt: dict, adapter, result_sink, cancel_event: asyncio.Event | None = None, instance_sink=None
) -> list[dict]:
    """校验锁定运行时后执行 PI，并在任何已创建实例路径上显式清理。"""

    manifest = attempt.get("manifest")
    digest = attempt.get("manifest_digest")
    if not isinstance(manifest, dict) or digest != compute_manifest_fingerprint(manifest):
        raise ValueError("PI attempt 缺少有效 Runtime Manifest")

    final_acked = False
    preserve_outputs = False
    primary_error: BaseException | None = None
    instance_id = await adapter.create(attempt)
    try:
        if instance_sink is not None:
            await _call_sink(instance_sink, instance_id)
        validate_pi_runtime(manifest, await adapter.inspect(instance_id))
        try:
            events = await _execute_until_cancelled(adapter, instance_id, attempt, cancel_event)
            refs: dict[str, dict] = {}
            for event in events:
                if cancel_event is not None and cancel_event.is_set():
                    raise PiExecutionCancelled("PI attempt cancelled before result ACK")
                envelope = build_pi_envelope(attempt=attempt, adapter_name=adapter.name, event=event)
                if "ref" in envelope:
                    await adapter.validate_ref(envelope["ref"])
                    existing_ref = refs.setdefault(envelope["type"], envelope["ref"])
                    if existing_ref != envelope["ref"]:
                        raise ValueError(f"PI {envelope['type']} ref 内容冲突")
                if envelope["type"] == "final":
                    for ref_type in ("artifact", "patch", "session"):
                        ref = envelope["payload"].get(ref_type)
                        if ref != refs.get(ref_type):
                            raise ValueError(f"PI final {ref_type} 与已回传 ref 不一致")
                        await adapter.validate_ref(ref)
                for sink_try in range(2):
                    try:
                        response = await _call_sink(result_sink, envelope)
                        break
                    except Exception:
                        if sink_try == 1:
                            raise
                if envelope["type"] == "final" and isinstance(response, dict) and response.get("ack") is True:
                    final_acked = True
            if not final_acked:
                raise RuntimeError("PI Runner 未收到 final ACK")
            preserve_outputs = True
            return events
        except PiExecutionCancelled:
            raise
        except Exception as exc:
            preserve_outputs = True
            raise PiExecutionUnknown(f"PI execution_unknown: {exc}") from exc
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            await adapter.stop(instance_id, preserve_outputs=preserve_outputs)
        except Exception as exc:
            raise PiCleanupFailed(f"PI cleanup_failed: {exc}", primary=primary_error) from exc


class LocalPiAdapter:
    """复用 provisioner 与 agent-sandbox 数据面的 attempt 独立 Local adapter。"""

    name = "local"

    def __init__(self, *, uid: str, run_id: str, attempt_id: str):
        self._uid = uid
        self._run_id = run_id
        self._attempt_id = str(attempt_id)
        identity = hashlib.sha256(f"{run_id}:{attempt_id}".encode()).hexdigest()[:24]
        self._scope = f"pi-{identity}"
        self._provider = get_sandbox_provider()
        self._backend: ProvisionerSandboxBackend | None = None
        self._stopped = False

    async def create(self, attempt: dict) -> str:
        """投影锁定 Skill 并创建 attempt 独立实例。"""

        if str(attempt.get("run_id")) != self._run_id or str(attempt.get("attempt_id")) != self._attempt_id:
            raise ValueError("Local PI adapter 与 attempt 归属不一致")
        await asyncio.to_thread(
            sync_thread_readable_skills,
            self._scope,
            ["pi-golden"],
            {"pi-golden": PI_GOLDEN_SKILL_DIR},
        )
        self._backend = ProvisionerSandboxBackend(
            thread_id=self._scope,
            uid=self._uid,
            file_thread_id=self._scope,
            skills_thread_id=self._scope,
            inherit_env=False,
        )
        return self._backend.id

    def _require_instance(self, instance_id: str) -> ProvisionerSandboxBackend:
        if self._backend is None or self._backend.id != instance_id or self._stopped:
            raise ValueError("Local PI instance 不可用")
        return self._backend

    async def inspect(self, instance_id: str) -> dict:
        """调用镜像内 Runner 回报实际 Node、PI、Runner 与 Skill digest。"""

        backend = self._require_instance(instance_id)
        result = await asyncio.to_thread(backend.execute, f"node {PI_RUNNER_SANDBOX_PATH} --inspect")
        if result.exit_code not in {0, None}:
            raise RuntimeError(result.output or "Local PI runtime inspect 失败")
        try:
            return json.loads((result.output or "").strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError("Local PI runtime inspect 返回无效 JSON") from exc

    async def execute(self, instance_id: str, job: dict) -> list[dict]:
        """在 Local sandbox 中执行 one-shot PI Runner 并解析 JSONL。"""

        backend = self._require_instance(instance_id)
        encoded = base64.urlsafe_b64encode(canonical_json(job).encode()).decode().rstrip("=")
        timeout = int(job["manifest"]["policy"]["timeout_seconds"])
        result = await asyncio.to_thread(
            backend.execute,
            f"node {PI_RUNNER_SANDBOX_PATH} {encoded}",
            timeout=timeout,
        )
        if result.exit_code not in {0, None}:
            raise RuntimeError(result.output or "Local PI Runner 执行失败")
        try:
            return [json.loads(line) for line in (result.output or "").splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise RuntimeError("Local PI Runner 返回无效 JSONL") from exc

    async def validate_ref(self, ref: dict) -> None:
        """ACK 前从服务器 outputs 回读 ref 并重算摘要。"""

        if not isinstance(ref, dict):
            raise ValueError("PI output ref 必须是对象")
        digest = ref.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("PI output ref 缺少 SHA-256")
        path = self.output_path(str(ref.get("path") or ""))
        if not path.is_file() or _file_digest(path) != digest:
            raise ValueError("PI output ref 文件缺失或摘要不匹配")

    def _cleanup_scope(self, *, preserve_outputs: bool) -> None:
        root = sandbox_user_data_dir(self._scope)
        if not root.exists():
            return
        for child in root.iterdir():
            if preserve_outputs and child.name == sandbox_outputs_dir(self._scope).name:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        if not any(root.iterdir()):
            root.rmdir()

    async def stop(self, instance_id: str, *, preserve_outputs: bool = False) -> None:
        """幂等删除实例 rootfs；宿主 outputs 保持可读。"""

        if self._stopped:
            return
        if self._backend is not None and self._backend.id != instance_id:
            raise ValueError("Local PI instance_id 不匹配")
        for attempt_no in range(2):
            try:
                await asyncio.to_thread(
                    self._provider.release,
                    self._scope,
                    uid=self._uid,
                    file_thread_id=self._scope,
                    skills_thread_id=self._scope,
                    clear_cache_on_delete_failure=False,
                )
                break
            except Exception:
                if attempt_no == 1:
                    raise
                await asyncio.sleep(0)
        await asyncio.to_thread(self._cleanup_scope, preserve_outputs=preserve_outputs)
        self._stopped = True

    async def instance_exists(self) -> bool:
        """供 E2E 断言 rootfs 已删除。"""

        connection = await asyncio.to_thread(
            self._provider.get,
            self._scope,
            uid=self._uid,
            file_thread_id=self._scope,
            skills_thread_id=self._scope,
        )
        return connection is not None

    def output_path(self, relative_path: str) -> Path:
        """解析服务器持久 outputs 内的 Runner 引用。"""

        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("PI output ref 不是安全相对路径")
        root = sandbox_outputs_dir(self._scope).resolve()
        path = root.joinpath(*relative.parts).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("PI output ref 逃逸服务器 outputs") from exc
        return path
