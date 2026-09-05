"""PI 运行时清单与执行 seam。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from yuxi.agents.backends.sandbox import ProvisionerSandboxBackend
from yuxi.agents.backends.sandbox.provider import get_sandbox_provider
from yuxi.agents.skills.service import compute_skill_dir_hash, is_valid_skill_slug, sync_user_accessible_skills
from yuxi.models.providers.cache import model_cache
from yuxi.services.agent_run_manifest_service import canonical_json, compute_manifest_fingerprint
from yuxi.utils import get_docker_safe_url
from yuxi.workspace.paths import ensure_bound_user_workdir
from yuxi.workspace.workdir import Workdir

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
PI_MODEL_APIS = {
    "anthropic": "anthropic-messages",
    "gemini": "google-generative-ai",
    "openai": "openai-completions",
    "openrouter": "openai-completions",
}
PI_AUTH_HEADERS = frozenset({"authorization", "x-api-key", "api-key", "x-goog-api-key"})
PI_MAX_OUTPUT_FILES = 200
PI_MAX_OUTPUT_FILE_BYTES = 64 * 1024 * 1024
PI_MAX_OUTPUT_BYTES = 256 * 1024 * 1024
PI_MAX_REF_BYTES = 16 * 1024 * 1024
PI_MAX_EVENT_STREAM_BYTES = 32 * 1024 * 1024


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


def resolve_pi_model_runtime(model_spec: str | None) -> tuple[dict, dict]:
    """把 Yuxi 聊天模型解析为 PI 的无凭据清单与临时凭据。"""

    info = model_cache.get_model_info(str(model_spec or ""))
    if info is None or info.model_type != "chat":
        raise ValueError(f"PI 执行模型不存在或不是聊天模型: {model_spec}")
    api = PI_MODEL_APIS.get(info.provider_type)
    if api is None:
        raise ValueError(f"PI 暂不支持 provider_type: {info.provider_type}")
    descriptor = {
        "provider_type": info.provider_type,
        "model_id": info.model_id,
        "display_name": info.display_name,
        "api": api,
        "base_url": get_docker_safe_url(info.base_url).rstrip("/"),
        "context_window": int(info.extra.get("context_window") or 128_000),
        "max_tokens": int(info.extra.get("max_tokens") or 32_768),
        "sampling_params": dict(info.request_body_overrides),
    }
    api_key = info.api_key.strip() if isinstance(info.api_key, str) else ""
    headers = dict(info.headers)
    has_header_auth = any(
        isinstance(name, str) and name.lower() in PI_AUTH_HEADERS and isinstance(value, str) and bool(value.strip())
        for name, value in headers.items()
    )
    if not api_key.strip() and not has_header_auth:
        raise ValueError("PI 执行模型缺少 API key 或受支持的认证请求头")
    credentials = {
        "api_key": api_key,
        "headers": headers,
        "auth_header": bool(api_key),
    }
    return descriptor, credentials


def build_pi_runtime_manifest(
    *,
    runner_path: Path,
    skill_dir: Path | None = None,
    skill_sources: dict[str, Path] | None = None,
    skill_runtime_paths: dict[str, str] | None = None,
    model: dict | None = None,
) -> tuple[dict, str]:
    """为 Local PI Task 构建不可变 Runtime Manifest。"""

    if not runner_path.is_file():
        raise ValueError(f"PI runner 不存在: {runner_path}")
    if skill_sources is None:
        if skill_dir is None or not skill_dir.is_dir():
            raise ValueError(f"PI Skill 不存在: {skill_dir}")
        skill_sources = {"pi-golden": skill_dir}
    runtime_paths = dict(skill_runtime_paths or {})
    skill_items = []
    for slug, source in sorted(skill_sources.items()):
        if not is_valid_skill_slug(slug) or not source.is_dir():
            raise ValueError(f"PI Skill 不存在或 slug 无效: {slug}")
        skill_items.append(
            {
                "slug": slug,
                "version": PI_GOLDEN_SKILL_VERSION if slug == "pi-golden" else "projected",
                "path": runtime_paths.get(slug) or f"/home/gem/skills/{slug}",
                "digest": compute_skill_dir_hash(source),
            }
        )
    if set(runtime_paths) - set(skill_sources):
        raise ValueError("PI Skill runtime 路径包含未知 slug")
    golden_bundle = len(skill_items) == 1 and skill_items[0]["slug"] == "pi-golden"
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
            "id": "pi-golden" if golden_bundle else "selected-skills",
            "version": PI_GOLDEN_SKILL_VERSION if golden_bundle else "1",
            "digest": hashlib.sha256(canonical_json(skill_items).encode()).hexdigest(),
            "items": skill_items,
            "read_only": all(item["path"].startswith("/home/gem/skills/") for item in skill_items),
        },
        "policy": {
            "timeout_seconds": 600 if model else 60,
            "tools": ["read", "bash", "edit", "write", "submit_artifact"] if model else ["read", "write"],
        },
    }
    if model:
        manifest["model"] = dict(model)
    return manifest, compute_manifest_fingerprint(manifest)


def build_default_pi_runtime_manifest(*, model: dict | None = None) -> tuple[dict, str]:
    """从随服务发布的审核资产构建 Local PI Runtime Manifest。"""

    return build_pi_runtime_manifest(runner_path=PI_RUNNER_PATH, skill_dir=PI_GOLDEN_SKILL_DIR, model=model)


def validate_pi_runtime(manifest: dict, actual: dict) -> None:
    """在 PI 启动前逐项比对 Runner、Node、PI 与 Skill 摘要。"""

    expected = {
        "runner_protocol": manifest["runner"]["protocol"],
        "runner_digest": manifest["runner"]["digest"],
        "pi_version": manifest["pi"]["version"],
        "pi_integrity": manifest["pi"]["integrity"],
        "node_version": manifest["node"]["version"],
    }
    for field, value in expected.items():
        if actual.get(field) != value:
            raise PiRuntimeMismatch(f"runtime_mismatch: {field}")
    actual_skills = actual.get("skills") if isinstance(actual.get("skills"), dict) else {}
    for item in manifest["skill_bundle"]["items"]:
        if actual_skills.get(item["path"]) != item["digest"]:
            raise PiRuntimeMismatch(f"runtime_mismatch: skill {item['slug']}")


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


async def _execute_until_cancelled(
    adapter,
    instance_id: str,
    attempt: dict,
    cancel_event: asyncio.Event | None,
    final_acked_event: asyncio.Event,
    event_sink,
):
    execute_kwargs = {"event_sink": event_sink} if getattr(adapter, "streams_events", False) else {}
    if cancel_event is None:
        return await adapter.execute(instance_id, attempt, **execute_kwargs)
    execute_task = asyncio.create_task(adapter.execute(instance_id, attempt, **execute_kwargs))
    cancel_task = asyncio.create_task(cancel_event.wait())
    done, _ = await asyncio.wait({execute_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
    if execute_task in done and (cancel_task not in done or final_acked_event.is_set()):
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        return execute_task.result()
    if final_acked_event.is_set():
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        return await execute_task
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
    final_acked_event = asyncio.Event()
    preserve_outputs = False
    primary_error: BaseException | None = None
    refs: dict[str, dict] = {}

    async def accept_event(event: dict) -> None:
        nonlocal final_acked, preserve_outputs
        if cancel_event is not None and cancel_event.is_set() and not final_acked_event.is_set():
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
            preserve_outputs = True
            final_acked_event.set()

    instance_id = await adapter.create(attempt)
    try:
        if instance_sink is not None:
            await _call_sink(instance_sink, instance_id)
        validate_pi_runtime(manifest, await adapter.inspect(instance_id))
        try:
            events = await _execute_until_cancelled(
                adapter,
                instance_id,
                attempt,
                cancel_event,
                final_acked_event,
                accept_event,
            )
            if not getattr(adapter, "streams_events", False):
                for event in events:
                    await accept_event(event)
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
    """复用 provisioner；普通 PI 隔离，Sandbox child 复用父执行实例。"""

    name = "local"
    streams_events = True

    def __init__(
        self,
        *,
        uid: str,
        run_id: str,
        attempt_id: str,
        runtime_scope_id: str | None = None,
        workdir_path: str | None = None,
        skill_sources: dict[str, Path] | None = None,
        reuse_sandbox: bool = False,
        credentials: dict | None = None,
    ):
        self._uid = str(uid)
        self._run_id = run_id
        self._attempt_id = str(attempt_id)
        identity = hashlib.sha256(f"{self._uid}:{run_id}:{attempt_id}".encode()).hexdigest()[:24]
        output_identity = hashlib.sha256(f"{run_id}:{attempt_id}".encode()).hexdigest()[:24]
        self._reuse_sandbox = reuse_sandbox
        self._scope = str(runtime_scope_id or "").strip() if reuse_sandbox else f"pi-{identity}"
        self._runtime_uid = self._uid if reuse_sandbox else self._scope
        self._workdir_path = (
            str(workdir_path or "").strip()
            if reuse_sandbox
            else f"projects/{uuid.uuid5(uuid.NAMESPACE_URL, f'yuxi-pi:{self._scope}')}"
        )
        if reuse_sandbox and (not self._scope or not self._workdir_path):
            raise ValueError("复用 PI sandbox 需要 runtime_scope_id 与 workdir_path")
        self._output_subdir = f"pi-runs/{output_identity}"
        self._skill_sources = dict(skill_sources or {})
        self._credentials = dict(credentials or {})
        self._workdir: Workdir | None = None
        self._provider = get_sandbox_provider()
        self._backend: ProvisionerSandboxBackend | None = None
        self._stopped = False

    async def create(self, attempt: dict) -> str:
        """投影 golden Skill，或复用父 Run 已创建的 Project Sandbox。"""

        if str(attempt.get("run_id")) != self._run_id or str(attempt.get("attempt_id")) != self._attempt_id:
            raise ValueError("Local PI adapter 与 attempt 归属不一致")
        items = attempt.get("manifest", {}).get("skill_bundle", {}).get("items", [])
        slugs = [str(item.get("slug") or "") for item in items if isinstance(item, dict)]
        if not slugs and not self._reuse_sandbox:
            slugs = ["pi-golden"]
        sources = dict(self._skill_sources)
        if slugs == ["pi-golden"] and not sources:
            sources = {"pi-golden": PI_GOLDEN_SKILL_DIR}
        if set(slugs) != set(sources):
            raise ValueError("PI attempt 的 Skill 清单与来源不一致")
        try:
            if not self._reuse_sandbox:
                await asyncio.to_thread(ensure_bound_user_workdir, self._runtime_uid, self._workdir_path)
            self._workdir = Workdir.open_existing(self._runtime_uid, self._workdir_path)
            if not self._reuse_sandbox:
                await asyncio.to_thread(sync_user_accessible_skills, self._runtime_uid, sources)
            self._backend = ProvisionerSandboxBackend(
                thread_id=self._scope,
                uid=self._runtime_uid,
                inherit_env=self._reuse_sandbox,
                create_if_missing=not self._reuse_sandbox,
                workdir_path=self._workdir_path,
            )
            if self._reuse_sandbox:
                await asyncio.to_thread(self._backend.ensure_available)
            return self._backend.id
        except BaseException as exc:
            if self._backend is not None:
                self._backend.close()
            if not self._reuse_sandbox:
                try:
                    await asyncio.to_thread(self._cleanup_scope, preserve_outputs=False)
                except Exception as cleanup_exc:
                    raise PiCleanupFailed(f"PI cleanup_failed: {cleanup_exc}", primary=exc) from cleanup_exc
            raise

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

    async def execute(self, instance_id: str, job: dict, *, event_sink=None) -> list[dict]:
        """在 Local sandbox 中执行 one-shot PI Runner 并解析 JSONL。"""

        backend = self._require_instance(instance_id)
        runner_job = dict(job)
        runner_job["output_subdir"] = self._output_subdir
        job_path = None
        try:
            if isinstance(job.get("manifest", {}).get("model"), dict):
                if not self._credentials:
                    raise RuntimeError("PI 执行模型缺少临时凭据")
                runner_job["credentials"] = self._credentials
                job_identity = hashlib.sha256(f"{self._run_id}:{self._attempt_id}".encode()).hexdigest()[:24]
                job_path = f"/home/gem/yuxi-secret-{job_identity}.json"
                await asyncio.to_thread(backend.write_ephemeral_secret, job_path, canonical_json(runner_job))
                command = f"node {PI_RUNNER_SANDBOX_PATH} --job {job_path}"
            else:
                encoded = base64.urlsafe_b64encode(canonical_json(runner_job).encode()).decode().rstrip("=")
                command = f"node {PI_RUNNER_SANDBOX_PATH} {encoded}"
            timeout = int(job["manifest"]["policy"]["timeout_seconds"])
            events: list[dict] = []
            invalid_lines: list[str] = []
            pending = ""

            async def consume_output(chunk: str) -> None:
                nonlocal pending
                pending += chunk
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        invalid_lines.append(line)
                        continue
                    events.append(event)
                    if event_sink is not None:
                        await _call_sink(event_sink, event)

            result = await backend.aexecute_stream(
                command,
                consume_output,
                timeout=timeout,
                max_output_bytes=PI_MAX_EVENT_STREAM_BYTES,
            )
            if result.exit_code not in {0, None}:
                raise RuntimeError(result.output or "Local PI Runner 执行失败")
            if pending.strip():
                await consume_output("\n")
            if invalid_lines:
                raise RuntimeError("Local PI Runner 返回无效 JSONL")
            return events
        finally:
            if job_path is not None:
                await asyncio.to_thread(backend.delete_ephemeral_secret, job_path)

    async def validate_ref(self, ref: dict) -> None:
        """ACK 前从服务器 outputs 回读 ref 并重算摘要。"""

        if not isinstance(ref, dict):
            raise ValueError("PI output ref 必须是对象")
        digest = ref.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("PI output ref 缺少 SHA-256")
        content = self.read_output(str(ref.get("path") or ""))
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("PI output ref 文件缺失或摘要不匹配")
        self._reject_persisted_credentials(content)

        files = ref.get("files")
        if files is None:
            return
        if not isinstance(files, list) or len(files) > PI_MAX_OUTPUT_FILES:
            raise ValueError("PI artifact 文件清单无效")
        if json.loads(content) != {"files": files}:
            raise ValueError("PI artifact 文件清单与持久 manifest 不匹配")
        seen = set()
        output_bytes = 0
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("PI artifact 文件项无效")
            item_digest = item.get("sha256")
            item_size = item.get("size")
            if (
                not isinstance(item_digest, str)
                or len(item_digest) != 64
                or type(item_size) is not int
                or not 0 <= item_size <= PI_MAX_OUTPUT_FILE_BYTES
            ):
                raise ValueError("PI artifact 文件缺失、大小或摘要不匹配")
            path = self._output_path(str(item.get("path") or ""))
            if path in seen:
                raise ValueError("PI artifact 文件清单含重复路径")
            seen.add(path)
            output_bytes += item_size
            if output_bytes > PI_MAX_OUTPUT_BYTES:
                raise ValueError("PI artifact 总大小超过 256 MiB")
            digest = hashlib.sha256()
            size = 0
            secrets = [self._credentials.get("api_key"), *(self._credentials.get("headers") or {}).values()]
            overlap = max((len(value.encode()) for value in secrets if isinstance(value, str)), default=0)
            tail = b""
            for chunk in self._workdir.iter_file_chunks(path, PI_MAX_OUTPUT_FILE_BYTES):
                size += len(chunk)
                digest.update(chunk)
                self._reject_persisted_credentials(tail + chunk)
                tail = (tail + chunk)[-(overlap - 1) :] if overlap > 1 else b""
            if size != item_size or digest.hexdigest() != item_digest:
                raise ValueError("PI artifact 文件缺失、大小或摘要不匹配")

    def _reject_persisted_credentials(self, content: bytes) -> None:
        """拒绝把本次模型凭据写入持久结果引用。"""

        values = [self._credentials.get("api_key"), *(self._credentials.get("headers") or {}).values()]
        if any(isinstance(value, str) and value and value.encode() in content for value in values):
            raise ValueError("PI 持久结果包含模型凭据")

    def _cleanup_scope(self, *, preserve_outputs: bool) -> None:
        cleanup_error: Exception | None = None
        try:
            workdir = self._workdir or Workdir.open_existing(self._runtime_uid, self._workdir_path)
        except FileNotFoundError:
            pass
        except Exception as exc:
            cleanup_error = exc
        else:
            self._workdir = workdir
            preserved = frozenset({"outputs"}) if preserve_outputs else frozenset()
            try:
                workdir.cleanup(preserve_directories=preserved)
            except Exception as exc:
                cleanup_error = exc
        try:
            sync_user_accessible_skills(self._runtime_uid, {})
        except Exception as exc:
            cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise cleanup_error

    async def stop(self, instance_id: str, *, preserve_outputs: bool = False) -> None:
        """关闭句柄；复用 child 不删除父实例，隔离运行仍完整清理。"""

        if self._stopped:
            return
        if self._backend is not None and self._backend.id != instance_id:
            raise ValueError("Local PI instance_id 不匹配")
        if self._backend is not None:
            self._backend.close()
        if self._reuse_sandbox:
            if not preserve_outputs:
                try:
                    workdir = self._workdir or Workdir.open_existing(self._runtime_uid, self._workdir_path)
                    workdir.delete(f"/outputs/{self._output_subdir}")
                except FileNotFoundError:
                    pass
            self._stopped = True
            return
        for attempt_no in range(2):
            try:
                await asyncio.to_thread(
                    self._provider.release,
                    self._scope,
                    uid=self._runtime_uid,
                    workdir_path=self._workdir_path,
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
            uid=self._runtime_uid,
            workdir_path=self._workdir_path,
        )
        return connection is not None

    def _output_path(self, relative_path: str) -> str:
        """把安全相对路径绑定到当前 attempt 交付目录。"""
        relative = PurePosixPath(relative_path)
        if (
            not relative.parts
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative_path.split("/"))
            or "\\" in relative_path
            or any(ord(char) < 32 or ord(char) == 127 for char in relative_path)
            or len(relative_path.encode()) > 1024
        ):
            raise ValueError("PI output ref 不是安全相对路径")
        if self._workdir is None:
            raise ValueError("Local PI Workdir 未初始化")
        return f"/outputs/{self._output_subdir}/{relative.as_posix()}"

    def read_output(self, relative_path: str) -> bytes:
        """有界读取小型 ref 文档；交付文件通过分块 capability 验证。"""
        output_path = self._output_path(relative_path)
        metadata = self._workdir.stat(output_path)
        if metadata["is_dir"]:
            raise ValueError("PI output ref 必须指向普通文件")
        if int(metadata["size"]) > PI_MAX_REF_BYTES:
            raise ValueError("PI output ref 超过 16 MiB")
        return b"".join(self._workdir.iter_file_chunks(output_path, PI_MAX_REF_BYTES))

    def workdir_exists(self) -> bool:
        """判断 attempt 的持久 Workdir 是否仍是有效目录。"""

        try:
            Workdir.open_existing(self._runtime_uid, self._workdir_path)
        except (FileNotFoundError, PermissionError, ValueError):
            return False
        return True

    @property
    def output_subdir(self) -> str:
        return self._output_subdir
