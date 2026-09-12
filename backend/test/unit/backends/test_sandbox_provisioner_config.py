from __future__ import annotations

import importlib.util
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient


MODULE_NAME = "sandbox_provisioner_app_for_test"


def _find_module_path() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "docker" / "sandbox_provisioner" / "app.py"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("docker/sandbox_provisioner/app.py not found from test path")


MODULE_PATH = _find_module_path()


def _load_module():
    existing = sys.modules.get(MODULE_NAME)
    if existing is not None:
        return existing

    spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _docker_backend(module, tmp_path, run_container):
    backend = object.__new__(module.LocalContainerProvisionerBackend)
    backend._lock = threading.RLock()
    backend._limits = module.SandboxResourceLimits()
    backend._container_port = 8080
    backend._network_prefix = "yuxi-know-sandbox"
    backend._sandbox_image = "sandbox-image"
    backend._container_prefix = "yuxi-sandbox"
    backend._sandbox_env = {}
    backend._health_timeout_seconds = 1
    backend._user_data_host_path = str(tmp_path)
    backend._skill_projections_host_path = str(tmp_path.parent / "skill-projections")
    backend._user_data_container_path = tmp_path
    backend._skill_projections_container_path = tmp_path.parent / "skill-projections"
    backend._user_data_container_path.mkdir(exist_ok=True)
    backend._skill_projections_container_path.mkdir(exist_ok=True)
    backend._client = SimpleNamespace(containers=SimpleNamespace(run=run_container, list=lambda **_kwargs: []))
    return backend


def test_directory_validation_identifies_missing_mount_prerequisite(tmp_path):
    module = _load_module()
    root = tmp_path / "skill-projections"
    root.mkdir()

    with pytest.raises(ValueError, match="^skill projection must reference"):
        module.LocalContainerProvisionerBackend._validate_directory_without_symlinks(
            root,
            ("fresh-user",),
            label="skill projection",
        )


def _docker_backend_with_running_container(monkeypatch, tmp_path):
    module = _load_module()
    captured = []

    class FakeContainer:
        name = "yuxi-sandbox-sandbox-1"
        status = "running"
        labels = {"thread-id": "thread-1"}
        attrs = {"State": {"Status": "running"}}

        def reload(self):
            return None

    backend = _docker_backend(
        module,
        tmp_path,
        lambda image, **kwargs: captured.append((image, kwargs)) or FakeContainer(),
    )
    (tmp_path / "shared/user-1/workspace").mkdir(parents=True, exist_ok=True)
    (tmp_path.parent / "skill-projections/user-1").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(backend, "_get_container", lambda _sandbox_id: None)
    monkeypatch.setattr(backend, "_ensure_network", backend._network_name)
    monkeypatch.setattr(module, "wait_for_sandbox_ready", lambda _url, timeout_seconds: True)
    return module, backend, captured


def test_canonical_backend_name(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()

    assert module.canonical_backend_name("docker") == "docker"
    assert module.canonical_backend_name("kubernetes") == "kubernetes"


def test_merged_sandbox_env_user_values_override_global(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()

    assert module.merged_sandbox_env(
        {"SHARED": "global", "GLOBAL_ONLY": "value"},
        {"SHARED": "user", "USER_ONLY": "value"},
    ) == {
        "SHARED": "user",
        "GLOBAL_ONLY": "value",
        "USER_ONLY": "value",
    }


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("MEMORY_MB", "0"),
        ("MEMORY_MB", "4"),
        ("MEMORY_MB", "4GiB"),
        ("CPUS", "0"),
        ("CPUS", "0.0001"),
        ("CPUS", "nan"),
        ("CPUS", "inf"),
        ("DOCKER_PIDS_LIMIT", "-1"),
        ("TMPFS_MB", "0"),
        ("TMPFS_MB", "4097"),
        ("MAX_INSTANCES", "0"),
        ("MAX_INSTANCES_PER_USER", "0"),
        ("MAX_INSTANCES_PER_USER", "7"),
    ],
)
def test_resource_limits_reject_invalid_environment(monkeypatch, variable, value):
    module = _load_module()
    monkeypatch.setenv(f"SANDBOX_{variable}", value)

    with pytest.raises(ValueError):
        module.SandboxResourceLimits.from_env()


def test_docker_creation_applies_resource_budget(monkeypatch, tmp_path):
    module, backend, captured = _docker_backend_with_running_container(monkeypatch, tmp_path)
    backend._limits = module.SandboxResourceLimits(memory_mb=6144, cpus=1.5, docker_pids_limit=300, tmpfs_mb=2048)

    backend.create("sandbox-1", "thread-1", "user-1")

    config = captured[0][1]
    assert config["mem_limit"] == config["memswap_limit"] == 6 * 1024**3
    assert config["nano_cpus"] == 1_500_000_000
    assert config["pids_limit"] == 300
    assert "size=2048m" in config["tmpfs"]["/home/gem"].split(",")


def _capacity_backend(monkeypatch, tmp_path, *, total=2, per_user=2):
    """用有状态容器集合验证准入后的真实 inventory 与复用行为。"""
    module = _load_module()
    inventory = {}

    class Container:
        def __init__(self, kwargs):
            self.name = kwargs["name"]
            self.id = self.name
            self.labels = kwargs["labels"]
            self.status = "running"
            self.attrs = {"State": {"Status": "running"}, "NetworkSettings": {"Networks": {kwargs["network"]: {}}}}
            self.attrs["HostConfig"] = {
                "Memory": kwargs["mem_limit"],
                "MemorySwap": kwargs["memswap_limit"],
                "NanoCpus": kwargs["nano_cpus"],
                "PidsLimit": kwargs["pids_limit"],
                "Tmpfs": kwargs["tmpfs"],
            }

        def reload(self):
            pass

    def run(_image, **kwargs):
        container = Container(kwargs)
        inventory[container.labels["sandbox-id"]] = container
        return container

    backend = _docker_backend(module, tmp_path, run)
    backend._limits = module.SandboxResourceLimits(max_instances=total, max_instances_per_user=per_user)
    backend._client.containers.list = lambda **_kwargs: list(inventory.values())
    monkeypatch.setattr(backend, "_get_container", inventory.get)
    monkeypatch.setattr(backend, "_ensure_network", backend._network_name)
    monkeypatch.setattr(module, "wait_for_sandbox_ready", lambda *_args, **_kwargs: True)
    return module, backend, inventory


def test_concurrent_creation_never_exceeds_capacity_and_reuses_identity(monkeypatch, tmp_path):
    module, backend, inventory = _capacity_backend(monkeypatch, tmp_path)

    def create(index):
        try:
            return backend.create(f"sandbox-{index}", f"thread-{index}", "user-1", inherit_env=False)
        except module.SandboxCapacityError as exc:
            return exc.detail

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(create, range(8)))

    assert len(inventory) == 2
    assert sum(isinstance(result, dict) and result["code"] == "sandbox_capacity_exhausted" for result in results) == 6
    first_id, first = next(iter(inventory.items()))
    reused = backend.create(first_id, first.labels["thread-id"], "user-1", inherit_env=False)
    assert reused.generation == first.id
    assert len(inventory) == 2
    with pytest.raises(ValueError, match="user identity"):
        backend.create(first_id, first.labels["thread-id"], "user-2", inherit_env=False)


def test_user_capacity_does_not_block_other_users(monkeypatch, tmp_path):
    module, backend, inventory = _capacity_backend(monkeypatch, tmp_path, total=3, per_user=1)
    backend.create("first", "thread-first", "user-1", inherit_env=False)

    with pytest.raises(module.SandboxCapacityError) as error:
        backend.create("second", "thread-second", "user-1", inherit_env=False)
    assert error.value.detail == {"code": "sandbox_capacity_exhausted", "scope": "user", "limit": 1}
    backend.create("third", "thread-third", "user-2", inherit_env=False)
    assert set(inventory) == {"first", "third"}


def test_docker_inventory_does_not_include_another_compose_slot(monkeypatch, tmp_path):
    module, backend, inventory = _capacity_backend(monkeypatch, tmp_path)
    backend.create("owned", "thread-owned", "user-1", inherit_env=False)
    foreign = SimpleNamespace(name="another-slot-sandbox-foreign", labels={"sandbox-id": "foreign", "uid": "user-1"})
    inventory["foreign"] = foreign

    assert [record.sandbox_id for record in backend.list()] == ["owned"]
    backend.create("second", "thread-second", "user-1", inherit_env=False)
    assert set(inventory) == {"owned", "foreign", "second"}


@pytest.mark.parametrize("field", ["Memory", "MemorySwap", "NanoCpus", "PidsLimit", "Tmpfs"])
def test_reuse_and_discovery_reject_old_resource_policy_without_deleting(monkeypatch, tmp_path, field):
    module, backend, inventory = _capacity_backend(monkeypatch, tmp_path)
    record = backend.create("existing", "thread", "user", inherit_env=False)
    inventory["existing"].attrs["HostConfig"][field] = {} if field == "Tmpfs" else 0

    with pytest.raises(module.SandboxResourcePolicyMismatchError, match="sandbox_resource_policy_mismatch"):
        backend.create("existing", "thread", "user", inherit_env=False)
    with pytest.raises(module.SandboxResourcePolicyMismatchError):
        backend.discover("existing")

    assert inventory["existing"].id == record.generation
    assert inventory["existing"].status == "running"


def test_docker_refuses_container_name_collision_with_foreign_ownership(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "docker.errors", SimpleNamespace(NotFound=LookupError))
    module, backend, _captured = _docker_backend_with_running_container(monkeypatch, tmp_path)
    foreign = SimpleNamespace(labels={"sandbox-id": "different-id", "managed-by": "yuxi-sandbox-provisioner"})
    backend._client.containers.get = lambda _name: foreign

    with pytest.raises(ValueError, match="unexpected ownership"):
        module.LocalContainerProvisionerBackend._get_container(backend, "expected-id")


def test_capacity_http_response_exposes_retryable_reason(monkeypatch):
    module = _load_module()
    monkeypatch.setenv("SANDBOX_PROVISIONER_TOKEN", "x" * 32)

    def create(*_args, **_kwargs):
        raise module.SandboxCapacityError("global", 6)

    monkeypatch.setattr(module, "backend_impl", SimpleNamespace(create=create))
    response = TestClient(module.app).post(
        "/api/sandboxes",
        headers={"Authorization": "Bearer " + "x" * 32},
        json={"sandbox_id": "sandbox", "thread_id": "thread", "uid": "user"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "sandbox_capacity_exhausted", "scope": "global", "limit": 6}}
    assert response.headers["Retry-After"] == "5"


def test_normalize_env_converts_values_to_strings(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()

    assert module.normalize_env({"A": 1, "B": None, "": "ignored"}) == {"A": "1", "B": ""}


def test_local_container_identity_validation_rejects_unsafe_path_segments(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    backend_cls = module.LocalContainerProvisionerBackend

    assert backend_cls._validate_thread_id("thread-1_2") == "thread-1_2"
    assert backend_cls._validate_uid("user-1_2") == "user-1_2"
    canonical_workdir = "projects/11111111-1111-4111-8111-111111111111"
    assert module.normalize_workdir_path(canonical_workdir) == canonical_workdir
    assert module.normalize_workdir_path("agents/skills") == "agents/skills"

    for value in ["../escape", "thread/name", "thread name", "thread;rm", "thread.name"]:
        with pytest.raises(ValueError):
            backend_cls._validate_thread_id(value)

    for value in ["../user", "user/name", "user name", "user;rm", "user.name"]:
        with pytest.raises(ValueError):
            backend_cls._validate_uid(value)

    for value in [
        "../workdir",
        "/workdir",
        "agents//skills",
        "https://example.com/workdir",
    ]:
        with pytest.raises(ValueError):
            module.normalize_workdir_path(value)


def test_docker_host_paths_require_explicit_storage_mounts(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    backend = object.__new__(module.LocalContainerProvisionerBackend)
    backend._user_data_host_path = None
    backend._skill_projections_host_path = None
    backend._client = SimpleNamespace(
        api=SimpleNamespace(
            inspect_container=lambda _container_id: {
                "Mounts": [
                    {"Destination": "/app/user-data", "Source": "/host/user-data"},
                    {"Destination": "/app/skill-projections", "Source": "/host/skill-projections"},
                ]
            }
        )
    )
    monkeypatch.setenv("HOSTNAME", "provisioner")

    backend._resolve_host_paths()

    assert backend._user_data_host_path == "/host/user-data"
    assert backend._skill_projections_host_path == "/host/skill-projections"

    backend._user_data_host_path = None
    backend._skill_projections_host_path = None
    backend._client.api.inspect_container = lambda _container_id: {
        "Mounts": [{"Destination": "/app/saves", "Source": "/host/legacy"}]
    }
    with pytest.raises(RuntimeError, match="explicit UserWorkspace/Skill"):
        backend._resolve_host_paths()


def test_memory_backend_accepts_runtime_thread_id(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    backend = module.MemoryProvisionerBackend()

    record = backend.create(
        "sandbox-1",
        "child-thread",
        "user-1",
    )

    assert record.sandbox_id == "sandbox-1"
    assert backend.discover("sandbox-1") is record


def test_memory_backend_concurrent_get_or_create_returns_one_generation(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    backend = module.MemoryProvisionerBackend()

    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(
            executor.map(
                lambda _index: backend.create(
                    "sandbox-shared",
                    "root-thread",
                    "user-1",
                    workdir_path="projects/11111111-1111-4111-8111-111111111111",
                ),
                range(32),
            )
        )

    assert len({id(record) for record in records}) == 1
    assert len({record.generation for record in records}) == 1
    assert records[0].workdir_path == "projects/11111111-1111-4111-8111-111111111111"

    with pytest.raises(ValueError, match="does not match"):
        backend.create(
            "sandbox-shared", "root-thread", "user-1", workdir_path="projects/22222222-2222-4222-8222-222222222222"
        )

    with pytest.raises(module.SandboxGenerationMismatchError, match="generation"):
        backend.delete("sandbox-shared", expected_generation="stale-generation")
    assert backend.discover("sandbox-shared") is records[0]

    backend.delete("sandbox-shared", expected_generation=records[0].generation)
    assert backend.discover("sandbox-shared") is None


def test_idle_reaper_does_not_delete_or_forget_new_generation(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()

    class FakeBackend:
        def __init__(self):
            self.generation = "generation-2"
            self.deleted = []

        def delete(self, sandbox_id, *, expected_generation=None):
            self.deleted.append((sandbox_id, expected_generation))
            if expected_generation != self.generation:
                raise module.SandboxGenerationMismatchError("stale generation")

    backend = FakeBackend()
    reaper = module.SandboxIdleReaper(backend)
    reaper.touch("sandbox-1", generation="generation-1")
    reaper._last_activity_at["sandbox-1"] = ("generation-1", 0)
    expired = reaper._collect_expired_sandboxes()
    reaper.touch("sandbox-1", generation="generation-2")

    reaper._delete_expired_sandbox(*expired[0])

    assert backend.deleted == []
    assert reaper._last_activity_at["sandbox-1"][0] == "generation-2"


def test_operation_pins_drain_started_requests_and_block_new_requests_during_delete(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    pins = module.SandboxOperationPins()
    delete_started = threading.Event()
    delete_finished = threading.Event()
    next_request_started = threading.Event()

    pins.acquire("sandbox-1")

    def delete_generation():
        pins.begin_delete("sandbox-1")
        delete_started.set()
        assert not next_request_started.is_set()
        pins.end_delete("sandbox-1")
        delete_finished.set()

    delete_thread = threading.Thread(target=delete_generation)
    delete_thread.start()
    with pins._condition:
        assert pins._condition.wait_for(lambda: "sandbox-1" in pins._deleting, timeout=1)
    assert not delete_started.is_set()

    def next_request():
        pins.acquire("sandbox-1")
        next_request_started.set()
        pins.release("sandbox-1")

    next_thread = threading.Thread(target=next_request)
    next_thread.start()
    assert not next_request_started.wait(timeout=0.05)

    pins.release("sandbox-1")
    assert delete_finished.wait(timeout=1)
    assert next_request_started.wait(timeout=1)
    delete_thread.join(timeout=1)
    next_thread.join(timeout=1)


def test_docker_mount_checks_reject_uploads_and_outputs_mounts(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    backend = object.__new__(module.LocalContainerProvisionerBackend)
    backend._user_data_host_path = str(tmp_path)
    backend._skill_projections_host_path = str(tmp_path.parent / "skill-projections")

    workspace = tmp_path / "shared" / "user-1" / "workspace"
    skills = tmp_path.parent / "skill-projections" / "user-1"
    container = SimpleNamespace(
        attrs={
            "Mounts": [
                {"Destination": "/home/gem/user-data", "Source": str(workspace)},
                {"Destination": "/home/gem/skills", "Source": str(skills), "RW": False, "Mode": "ro"},
            ]
        }
    )

    assert backend._has_expected_user_data_mounts(container, "user-1") is True
    assert backend._is_expected_skills_mount(container, "user-1") is True
    assert backend._is_expected_skills_mount(container, "user-2") is False

    container.attrs["Mounts"][1]["RW"] = True
    assert backend._is_expected_skills_mount(container, "user-1") is False
    container.attrs["Mounts"][1]["RW"] = False

    container.attrs["Mounts"].append(
        {"Destination": "/home/gem/user-data/outputs", "Source": str(tmp_path / "legacy-outputs")}
    )
    assert backend._has_expected_user_data_mounts(container, "user-1") is False
    container.attrs["Mounts"].pop()

    container.attrs["Mounts"].append(
        {"Destination": "/home/gem/user-data/uploads", "Source": str(tmp_path / "legacy-uploads")}
    )
    assert backend._has_expected_user_data_mounts(container, "user-1") is False


def test_docker_sandbox_mounts_shared_workspace_without_thread_history_projection(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module, backend, captured = _docker_backend_with_running_container(monkeypatch, tmp_path)

    backend.create("sandbox-1", "thread-1", "user-1")

    volumes = captured[0][1]["volumes"]
    destinations = {mount["bind"] for mount in volumes.values()}
    assert destinations == {
        "/home/gem/user-data",
        "/home/gem/skills",
    }
    assert all("/agents/chats" not in destination for destination in destinations)


def test_docker_project_workdir_contract_mounts_shared_posix_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module, backend, captured = _docker_backend_with_running_container(monkeypatch, tmp_path)

    workdir = tmp_path / "shared" / "user-1" / "workspace" / "projects" / "11111111-1111-4111-8111-111111111111"
    workdir.mkdir(parents=True)
    record = backend.create(
        "sandbox-project", "root-thread", "user-1", workdir_path="projects/11111111-1111-4111-8111-111111111111"
    )

    run_kwargs = captured[0][1]
    destinations = {mount["bind"] for mount in run_kwargs["volumes"].values()}
    assert destinations == {
        "/home/gem/user-data",
        "/home/gem/skills",
    }
    assert run_kwargs["working_dir"] == "/home/gem/user-data/projects/11111111-1111-4111-8111-111111111111"
    assert run_kwargs["labels"]["workdir-path"] == "projects/11111111-1111-4111-8111-111111111111"
    assert {key: run_kwargs["environment"][key] for key in ("USER", "USER_UID", "USER_GID")} == {
        "USER": "gem",
        "USER_UID": "1000",
        "USER_GID": "1000",
    }
    assert record.workdir_path is None  # Fake container has no Docker labels; real discover reads the label.


def test_docker_rejects_symlink_in_workspace_identity_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    _module, backend, captured = _docker_backend_with_running_container(monkeypatch, tmp_path)
    shared = backend._user_data_container_path / "shared"
    outside = tmp_path.parent / "outside-user-workspace"
    shared.mkdir(exist_ok=True)
    outside.mkdir()
    (shared / "user-1/workspace").rmdir()
    (shared / "user-1").rmdir()
    (shared / "user-1").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="without symlinks"):
        backend.create("sandbox-project", "root-thread", "user-1")

    assert captured == []


def test_docker_rejects_rebinding_existing_runtime_to_another_workdir(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()

    class FakeContainer:
        status = "running"
        labels = {
            "uid": "user-1",
            "thread-id": "root-thread",
            "workdir-path": "projects/11111111-1111-4111-8111-111111111111",
        }
        attrs = {"State": {"Status": "running"}}

        def reload(self):
            return None

    backend = _docker_backend(module, tmp_path, lambda *_args, **_kwargs: pytest.fail("sandbox was recreated"))
    monkeypatch.setattr(backend, "_get_container", lambda _sandbox_id: FakeContainer())

    with pytest.raises(ValueError, match="workdir identity"):
        backend.create(
            "sandbox-project", "root-thread", "user-1", workdir_path="projects/22222222-2222-4222-8222-222222222222"
        )


def test_kubernetes_mount_check_rejects_uploads_and_outputs_mounts(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._user_data_pvc = "user-data"
    backend._skill_pvc = "skills"
    pod = SimpleNamespace(
        spec=SimpleNamespace(
            volumes=[
                SimpleNamespace(
                    name="user-data",
                    persistent_volume_claim=SimpleNamespace(claim_name="user-data"),
                ),
                SimpleNamespace(
                    name="skills-data",
                    persistent_volume_claim=SimpleNamespace(claim_name="skills"),
                ),
            ],
            containers=[
                SimpleNamespace(
                    name="sandbox",
                    volume_mounts=[
                        SimpleNamespace(
                            mount_path="/home/gem/user-data",
                            name="user-data",
                            sub_path="shared/user-1/workspace",
                        ),
                        SimpleNamespace(
                            mount_path="/home/gem/skills",
                            name="skills-data",
                            sub_path="skill-projections/user-1",
                            read_only=True,
                        ),
                    ],
                )
            ],
        )
    )

    assert backend._pod_has_expected_mounts(
        pod,
        uid="user-1",
    )
    pod.spec.containers[0].volume_mounts[1].read_only = False
    assert not backend._pod_has_expected_mounts(
        pod,
        uid="user-1",
    )
    pod.spec.containers[0].volume_mounts[1].read_only = True
    pod.spec.containers[0].volume_mounts.append(
        SimpleNamespace(
            mount_path="/home/gem/user-data/outputs",
            sub_path="threads/parent-thread/user-data/outputs",
        )
    )
    assert not backend._pod_has_expected_mounts(
        pod,
        uid="user-1",
    )
    pod.spec.containers[0].volume_mounts.pop()

    pod.spec.containers[0].volume_mounts.append(
        SimpleNamespace(
            mount_path="/home/gem/user-data/uploads",
            sub_path="threads/parent-thread/user-data/uploads",
        )
    )
    assert not backend._pod_has_expected_mounts(pod, uid="user-1")
    pod.spec.containers[0].volume_mounts.pop()

    pod.spec.volumes[0].persistent_volume_claim.claim_name = "old-user-data"
    assert not backend._pod_has_expected_mounts(pod, uid="user-1")


def test_ephemeral_mount_validation_rejects_exact_projects_root(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    container = SimpleNamespace(attrs={"Mounts": [{"Destination": "/home/gem/projects"}]})

    assert not module.LocalContainerProvisionerBackend._has_no_persistent_file_mounts(container)

    backend = object.__new__(module.KubernetesProvisionerBackend)
    pod = SimpleNamespace(
        spec=SimpleNamespace(
            volumes=[],
            containers=[
                SimpleNamespace(
                    name="sandbox",
                    volume_mounts=[SimpleNamespace(mount_path="/home/gem/projects")],
                )
            ],
        )
    )
    assert not backend._pod_has_expected_mounts(
        pod,
        uid="remote-skill-user",
        ephemeral_storage=True,
    )


def test_management_api_requires_bearer_token(monkeypatch):
    token = "test-provisioner-token-that-is-long-enough"
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    monkeypatch.setenv("SANDBOX_PROVISIONER_TOKEN", token)
    module = _load_module()

    with TestClient(module.app) as client:
        assert client.get("/api/sandboxes").status_code == 401
        assert client.get("/api/sandboxes", headers={"Authorization": "Bearer wrong"}).status_code == 401

        response = client.get(
            "/api/sandboxes",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"sandboxes": [], "count": 0}


def test_authenticated_management_api_returns_proxied_sandbox_url(monkeypatch):
    token = "test-provisioner-token-that-is-long-enough"
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    monkeypatch.setenv("SANDBOX_PROVISIONER_TOKEN", token)
    monkeypatch.setenv("PROVISIONER_PUBLIC_URL", "http://sandbox-provisioner:8002")
    module = _load_module()
    headers = {"Authorization": f"Bearer {token}"}
    sandbox_id = "sandbox-auth-test"

    with TestClient(module.app) as client:
        create_response = client.post(
            "/api/sandboxes",
            headers=headers,
            json={
                "sandbox_id": sandbox_id,
                "thread_id": "thread-1",
                "uid": "user-1",
            },
        )
        list_response = client.get("/api/sandboxes", headers=headers)
        stale_delete_response = client.delete(
            f"/api/sandboxes/{sandbox_id}",
            headers=headers,
            params={"expected_generation": "stale-generation"},
        )
        delete_response = client.delete(
            f"/api/sandboxes/{sandbox_id}",
            headers=headers,
            params={"expected_generation": create_response.json()["generation"]},
        )

    expected_url = f"http://sandbox-provisioner:8002/api/sandboxes/{sandbox_id}/proxy"
    assert create_response.status_code == 200
    assert create_response.json()["sandbox_url"] == expected_url
    assert list_response.status_code == 200
    sandboxes = list_response.json()["sandboxes"]
    assert len(sandboxes) == 1
    assert sandboxes[0]["sandbox_id"] == sandbox_id
    assert sandboxes[0]["sandbox_url"] == expected_url
    assert sandboxes[0]["status"] == "Running"
    assert sandboxes[0]["generation"]
    assert sandboxes[0]["workdir_path"] is None
    assert stale_delete_response.status_code == 409
    assert delete_response.status_code == 200


def test_create_sandbox_forwards_environment_policy(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    calls = []

    def create(*_args, **kwargs):
        calls.append(kwargs)
        return module.SandboxRecord(sandbox_id="sandbox-1", sandbox_url="http://sandbox", status="Running")

    monkeypatch.setattr(module, "backend_impl", SimpleNamespace(create=create))
    monkeypatch.setattr(module.idle_reaper, "touch", lambda _sandbox_id, **_kwargs: None)

    module.create_sandbox(
        module.CreateSandboxRequest(
            sandbox_id="sandbox-1",
            thread_id="thread-1",
            uid="user-1",
            inherit_env=False,
        )
    )

    assert calls[0]["inherit_env"] is False


@pytest.mark.parametrize(("status_code", "content_range"), [(200, None), (206, "bytes 2-7/8"), (416, "bytes */8")])
def test_authenticated_proxy_forwards_request_without_management_token(monkeypatch, status_code, content_range):
    token = "test-provisioner-token-that-is-long-enough"
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    monkeypatch.setenv("SANDBOX_PROVISIONER_TOKEN", token)
    module = _load_module()
    headers = {"Authorization": f"Bearer {token}"}
    captured = []

    def upstream(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        response_headers = {"X-Ignored": "value", "Accept-Ranges": "bytes"}
        if content_range is not None:
            response_headers["Content-Range"] = content_range
        return httpx.Response(status_code, json={"ok": True}, headers=response_headers)

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(upstream)

    clients = []

    def create_client(**kwargs):
        client = real_async_client(transport=transport, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        create_client,
    )

    with TestClient(module.app) as client:
        client.post(
            "/api/sandboxes",
            headers=headers,
            json={
                "sandbox_id": "sandbox-proxy-test",
                "thread_id": "thread-1",
                "uid": "user-1",
            },
        )
        response = client.get(
            "/api/sandboxes/sandbox-proxy-test/proxy/v1/sandbox",
            headers=headers,
            params={"detail": "full"},
        )
        second_response = client.get(
            "/api/sandboxes/sandbox-proxy-test/proxy/v1/sandbox",
            headers=headers,
        )

    assert response.status_code == status_code
    assert response.json() == {"ok": True}
    assert second_response.status_code == status_code
    assert response.headers.get("content-range") == content_range
    assert response.headers["accept-ranges"] == "bytes"
    assert len(clients) == 1
    assert clients[0].is_closed
    assert str(captured[0].url) == "http://agent-sandbox:8000/v1/sandbox?detail=full"
    assert str(captured[1].url) == "http://agent-sandbox:8000/v1/sandbox"
    assert "authorization" not in captured[0].headers
    assert "x-ignored" not in response.headers


@pytest.mark.asyncio
async def test_proxy_discovers_sandbox_outside_event_loop_thread(monkeypatch):
    token = "test-provisioner-token-that-is-long-enough"
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    monkeypatch.setenv("SANDBOX_PROVISIONER_TOKEN", token)
    module = _load_module()
    event_loop_thread = threading.get_ident()
    discover_threads = []

    def discover(sandbox_id):
        discover_threads.append(threading.get_ident())
        return module.SandboxRecord(
            sandbox_id=sandbox_id,
            sandbox_url="http://agent-sandbox:8000",
            status="Running",
        )

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"ok": True}))
    http_client = real_async_client(transport=transport, timeout=None, follow_redirects=False, trust_env=False)
    module.app.state.http_client = http_client
    monkeypatch.setattr(module, "backend_impl", SimpleNamespace(discover=discover))
    request = module.Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/sandboxes/sandbox-proxy-test/proxy/v1/sandbox",
            "headers": [],
            "query_string": b"",
            "app": module.app,
        },
        receive,
    )

    try:
        response = await module.proxy_sandbox_request("sandbox-proxy-test", request, "v1/sandbox")
        body = b"".join([chunk async for chunk in response.body_iterator])
    finally:
        await http_client.aclose()

    assert body == b'{"ok":true}'
    assert discover_threads and discover_threads[0] != event_loop_thread


def test_docker_backend_uses_private_network_without_published_port(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    _, backend, captured = _docker_backend_with_running_container(monkeypatch, tmp_path)

    record = backend.create("sandbox-1", "thread-1", "user-1")

    assert record.sandbox_url == "http://yuxi-sandbox-sandbox-1:8080"
    assert captured[0][0] == "sandbox-image"
    assert captured[0][1]["network"] == "yuxi-know-sandbox-sandbox-1"
    assert "ports" not in captured[0][1]


def test_docker_ephemeral_sandbox_has_only_runtime_identity_environment_and_no_persistent_mounts(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    _, backend, captured = _docker_backend_with_running_container(monkeypatch, tmp_path)
    backend._sandbox_env = {"GLOBAL_SECRET": "value"}
    uid = "remote-skill-ephemeral"

    backend.create("sandbox-1", "thread-1", uid, {"USER_SECRET": "value"}, inherit_env=False)

    run_config = captured[0][1]
    assert run_config["environment"] == {"USER": "gem", "USER_UID": "1000", "USER_GID": "1000"}
    assert run_config["volumes"] == {}
    assert run_config["labels"]["storage-mode"] == "ephemeral"
    assert not (backend._user_data_container_path / "shared" / uid).exists()
    assert not (backend._skill_projections_container_path / uid).exists()


def test_kubernetes_ephemeral_sandbox_uses_only_empty_home(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()

    class FakeKubernetesClient:
        def __getattr__(self, _name):
            return lambda *_args, **kwargs: SimpleNamespace(**kwargs)

    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._client = FakeKubernetesClient()
    backend._sandbox_image = "sandbox-image"
    backend._limits = module.SandboxResourceLimits()
    backend._container_port = 8080
    backend._user_data_pvc = "threads"
    backend._skill_pvc = "skills"
    backend._sandbox_env = {"GLOBAL_SECRET": "value"}

    pod = backend._build_pod_spec(
        "sandbox-1",
        "thread-1",
        "user-1",
        {"USER_SECRET": "value"},
        inherit_env=False,
    )

    assert pod.spec.automount_service_account_token is False
    assert {item.name: item.value for item in pod.spec.containers[0].env} == {
        "USER": "gem",
        "USER_UID": "1000",
        "USER_GID": "1000",
    }
    sandbox_mounts = {mount.mount_path for mount in pod.spec.containers[0].volume_mounts}
    assert sandbox_mounts == {"/home/gem"}
    assert pod.spec.init_containers == []
    assert [volume.name for volume in pod.spec.volumes] == ["home-dir"]
    assert pod.metadata.annotations["storage-mode"] == "ephemeral"
    assert backend._pod_has_expected_mounts(
        pod,
        uid="user-1",
        ephemeral_storage=True,
    )
    pod.spec.containers[0].volume_mounts.append(
        SimpleNamespace(mount_path="/home/gem/skills", name="skills-data", sub_path="skill-projections/user-1")
    )
    assert not backend._pod_has_expected_mounts(
        pod,
        uid="user-1",
        ephemeral_storage=True,
    )


def test_kubernetes_workdir_contract_uses_user_workspace_subpath(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()

    class FakeKubernetesClient:
        def __getattr__(self, _name):
            return lambda *_args, **kwargs: SimpleNamespace(**kwargs)

    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._client = FakeKubernetesClient()
    backend._sandbox_image = "sandbox-image"
    backend._limits = module.SandboxResourceLimits()
    backend._container_port = 8080
    backend._user_data_pvc = "threads-rwx"
    backend._skill_pvc = "skills-rwx"
    backend._sandbox_env = {}

    pod = backend._build_pod_spec(
        "sandbox-1",
        "root-thread",
        "user-1",
        {},
        inherit_env=False,
        workdir_path="projects/11111111-1111-4111-8111-111111111111",
    )

    sandbox = pod.spec.containers[0]
    mounts = {mount.mount_path: getattr(mount, "sub_path", None) for mount in sandbox.volume_mounts}
    assert sandbox.working_dir == "/home/gem/user-data/projects/11111111-1111-4111-8111-111111111111"
    assert sandbox.resources.limits == {"memory": "4096Mi", "cpu": "2.0"}
    assert sandbox.resources.requests == sandbox.resources.limits
    home = next(volume for volume in pod.spec.volumes if volume.name == "home-dir")
    assert (home.empty_dir.medium, home.empty_dir.size_limit) == ("Memory", "1024Mi")
    assert mounts["/home/gem/user-data"] == "shared/user-1/workspace"
    assert mounts["/home/gem/skills"] == "skill-projections/user-1"
    skills_mount = next(mount for mount in sandbox.volume_mounts if mount.mount_path == "/home/gem/skills")
    assert skills_mount.read_only is True
    assert skills_mount.name == "skills-data"
    claims = {
        volume.name: volume.persistent_volume_claim.claim_name
        for volume in pod.spec.volumes
        if getattr(volume, "persistent_volume_claim", None) is not None
    }
    assert claims == {"user-data": "threads-rwx", "skills-data": "skills-rwx"}
    assert pod.metadata.annotations["workdir-path"] == "projects/11111111-1111-4111-8111-111111111111"
    assert pod.metadata.labels["managed-by"] == "yuxi-sandbox-provisioner"
    assert pod.spec.security_context.run_as_user == 0
    assert getattr(pod.spec.security_context, "fs_group", None) is None
    sandbox_env = {item.name: item.value for item in sandbox.env}
    assert {key: sandbox_env[key] for key in ("USER", "USER_UID", "USER_GID")} == {
        "USER": "gem",
        "USER_UID": "1000",
        "USER_GID": "1000",
    }
    init = pod.spec.init_containers[0]
    assert init.resources.limits == sandbox.resources.limits
    assert init.command == ["python", "-c"]
    assert "os.O_NOFOLLOW" in init.args[0]
    assert "('projects', '11111111-1111-4111-8111-111111111111')" in init.args[0]
    assert "MARKER_DIR = '.v072-runtime-identity'" in init.args[0]
    assert "os.mkdir(part, 0o700" in init.args[0]
    assert "os.fchmod(fd, 0o700)" in init.args[0]
    assert "follow_symlinks=False" in init.args[0]
    assert "'uploads'" not in init.args[0]
    assert "'outputs'" not in init.args[0]
    compile(init.args[0], "<kubernetes-storage-init>", "exec")


def test_kubernetes_storage_init_migrates_only_real_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    user_data = tmp_path / "user-data"
    skills_data = tmp_path / "skills-data"
    user_data.mkdir()
    skills_data.mkdir()
    workspace = user_data / "shared/user-1/workspace"
    workspace.mkdir(parents=True)
    (workspace / "projects/11111111-1111-4111-8111-111111111111").mkdir(parents=True)
    document = workspace / "old.txt"
    document.write_text("old", encoding="utf-8")
    document.chmod(0o666)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_mode = outside.stat().st_mode & 0o777
    (workspace / "linked").symlink_to(outside, target_is_directory=True)

    script = module.kubernetes_storage_init_script("user-1", "projects/11111111-1111-4111-8111-111111111111")
    script = script.replace("/mnt/user-data", str(user_data)).replace("/mnt/skills-data", str(skills_data))
    script = script.replace("UID = 1000", f"UID = {os.getuid()}").replace("GID = 1000", f"GID = {os.getgid()}")

    exec(compile(script, "<kubernetes-storage-init>", "exec"), {})

    assert document.stat().st_mode & 0o777 == 0o600
    assert (workspace / "linked").is_symlink()
    assert outside.stat().st_mode & 0o777 == outside_mode
    assert (workspace / "projects/11111111-1111-4111-8111-111111111111").is_dir()
    assert (skills_data / "skill-projections/user-1").is_dir()
    assert (user_data / ".v072-runtime-identity/workspace-user-1").exists()
    assert (skills_data / ".v072-runtime-identity/skills-user-1").exists()


def test_kubernetes_storage_init_rejects_missing_authoritative_workdir(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    user_data = tmp_path / "user-data"
    skills_data = tmp_path / "skills-data"
    user_data.mkdir()
    skills_data.mkdir()
    script = module.kubernetes_storage_init_script("user-1", "projects/missing")
    script = script.replace("/mnt/user-data", str(user_data)).replace("/mnt/skills-data", str(skills_data))
    script = script.replace("UID = 1000", f"UID = {os.getuid()}").replace("GID = 1000", f"GID = {os.getgid()}")

    with pytest.raises(FileNotFoundError):
        exec(compile(script, "<kubernetes-storage-init>", "exec"), {})

    assert not (user_data / "shared/user-1/workspace/projects/missing").exists()


def test_kubernetes_rejects_rebinding_existing_runtime_to_another_workdir(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._lock = threading.Lock()
    backend._core_api = SimpleNamespace()
    kubernetes_module = ModuleType("kubernetes")
    client_module = ModuleType("kubernetes.client")
    rest_module = ModuleType("kubernetes.client.rest")

    class ApiException(Exception):
        pass

    rest_module.ApiException = ApiException
    client_module.rest = rest_module
    kubernetes_module.client = client_module
    monkeypatch.setitem(sys.modules, "kubernetes", kubernetes_module)
    monkeypatch.setitem(sys.modules, "kubernetes.client", client_module)
    monkeypatch.setitem(sys.modules, "kubernetes.client.rest", rest_module)
    monkeypatch.setattr(
        backend,
        "discover",
        lambda _sandbox_id: module.SandboxRecord(
            sandbox_id="sandbox-1",
            sandbox_url="http://sandbox",
            generation="generation-1",
            workdir_path="projects/11111111-1111-4111-8111-111111111111",
        ),
    )
    monkeypatch.setattr(backend, "_discovered_matches_request", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(backend, "delete", lambda *_args, **_kwargs: pytest.fail("sandbox was deleted"))

    with pytest.raises(ValueError, match="identity does not match"):
        backend.create(
            "sandbox-1", "root-thread", "user-1", workdir_path="projects/22222222-2222-4222-8222-222222222222"
        )


@pytest.mark.parametrize("changed", ["limits", "requests", "init_limits", "home_size", "home_medium"])
def test_kubernetes_resource_policy_reads_pod_and_preserves_running_generation(monkeypatch, changed):
    module = _load_module()
    # SDK 是 provisioner 独立镜像的依赖；unit 固定外部 quantity oracle，真实 SDK 另在该镜像验证。
    quantities = {"0": 0, "2.0": 2, "2000m": 2, "4Gi": 4294967296, "1Gi": 1073741824}
    monkeypatch.setitem(sys.modules, "kubernetes.client.rest", SimpleNamespace(ApiException=RuntimeError))
    monkeypatch.setitem(
        sys.modules, "kubernetes.utils.quantity", SimpleNamespace(parse_quantity=lambda v: Decimal(quantities[str(v)]))
    )

    class Client:
        def __getattr__(self, _name):
            return lambda **kwargs: SimpleNamespace(**kwargs)

    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._client = Client()
    backend._lock = threading.RLock()
    backend._limits = module.SandboxResourceLimits()
    backend._namespace = "isolated-slot"
    backend._node_host = "node"
    backend._sandbox_image = "sandbox-image"
    backend._sandbox_env = {}
    backend._container_port = 8080
    backend._user_data_pvc = "users"
    backend._skill_pvc = "skills"
    pod = backend._build_pod_spec("existing", "thread", "user", {}, inherit_env=True)
    pod.metadata.uid = "original-generation"
    pod.status = SimpleNamespace(phase="Running")
    for container in pod.spec.containers + pod.spec.init_containers:
        container.resources = SimpleNamespace(
            limits={"memory": "4Gi", "cpu": "2000m"}, requests={"memory": "4Gi", "cpu": "2000m"}
        )
    home = next(volume.empty_dir for volume in pod.spec.volumes if volume.name == "home-dir")
    home.size_limit = "1Gi"
    backend._core_api = SimpleNamespace(
        read_namespaced_pod=lambda **_kwargs: pod,
        read_namespaced_service=lambda **_kwargs: SimpleNamespace(
            spec=SimpleNamespace(ports=[SimpleNamespace(node_port=8080)])
        ),
    )
    before = backend.create("existing", "thread", "user")
    assert before.generation == pod.metadata.uid

    if changed in {"limits", "requests"}:
        setattr(pod.spec.containers[0].resources, changed, {})
    elif changed == "init_limits":
        pod.spec.init_containers[0].resources.limits = {}
    elif changed == "home_size":
        home.size_limit = None
    else:
        home.medium = ""

    with pytest.raises(module.SandboxResourcePolicyMismatchError):
        backend.discover("existing")
    with pytest.raises(module.SandboxResourcePolicyMismatchError):
        backend.create("existing", "thread", "user")
    with pytest.raises(module.SandboxResourcePolicyMismatchError):
        backend._discovered_matches_request(
            "existing", thread_id="thread", uid="user", workdir_path=None, ephemeral_storage=False
        )
    assert pod.metadata.uid == before.generation
    assert pod.status.phase == "Running"


def test_kubernetes_pod_conflict_is_revalidated_before_creating_service(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    kubernetes_module = ModuleType("kubernetes")
    client_module = ModuleType("kubernetes.client")
    rest_module = ModuleType("kubernetes.client.rest")

    class ApiException(Exception):
        def __init__(self, status=None):
            self.status = status

    rest_module.ApiException = ApiException
    client_module.rest = rest_module
    kubernetes_module.client = client_module
    monkeypatch.setitem(sys.modules, "kubernetes", kubernetes_module)
    monkeypatch.setitem(sys.modules, "kubernetes.client", client_module)
    monkeypatch.setitem(sys.modules, "kubernetes.client.rest", rest_module)

    class FakeCoreApi:
        def list_namespaced_pod(self, **_kwargs):
            return SimpleNamespace(items=[])

        def create_namespaced_pod(self, **_kwargs):
            raise ApiException(status=409)

        def create_namespaced_service(self, **_kwargs):
            pytest.fail("service was created before pod identity validation")

    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._lock = threading.RLock()
    backend._core_api = FakeCoreApi()
    backend._limits = module.SandboxResourceLimits()
    backend._namespace = "yuxi"
    backend._pod_name = lambda _sandbox_id: "pod-1"
    backend._service_name = lambda _sandbox_id: "service-1"
    backend.discover = lambda _sandbox_id: None
    backend._build_pod_spec = lambda *_args, **_kwargs: SimpleNamespace()
    backend._discovered_matches_request = lambda *_args, **_kwargs: False

    with pytest.raises(ValueError, match="identity does not match"):
        backend.create(
            "sandbox-1", "root-thread", "user-1", workdir_path="projects/22222222-2222-4222-8222-222222222222"
        )


def test_kubernetes_delete_uses_uid_generation_precondition(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    kubernetes_module = ModuleType("kubernetes")
    client_module = ModuleType("kubernetes.client")
    rest_module = ModuleType("kubernetes.client.rest")

    class ApiException(Exception):
        def __init__(self, status=None):
            self.status = status

    rest_module.ApiException = ApiException
    client_module.rest = rest_module
    kubernetes_module.client = client_module
    monkeypatch.setitem(sys.modules, "kubernetes", kubernetes_module)
    monkeypatch.setitem(sys.modules, "kubernetes.client", client_module)
    monkeypatch.setitem(sys.modules, "kubernetes.client.rest", rest_module)

    calls = []

    class FakeCoreApi:
        def delete_namespaced_pod(self, **kwargs):
            calls.append(("pod", kwargs))

        def delete_namespaced_service(self, **kwargs):
            calls.append(("service", kwargs))

    class FakeKubernetesClient:
        @staticmethod
        def V1Preconditions(**kwargs):
            return SimpleNamespace(**kwargs)

        @staticmethod
        def V1DeleteOptions(**kwargs):
            return SimpleNamespace(**kwargs)

    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._core_api = FakeCoreApi()
    backend._client = FakeKubernetesClient()
    backend._lock = threading.RLock()
    backend._namespace = "yuxi"
    backend._pod_name = lambda _sandbox_id: "pod-1"
    backend._service_name = lambda _sandbox_id: "service-1"

    backend.delete("sandbox-1", expected_generation="pod-uid-1")

    assert [kind for kind, _kwargs in calls] == ["pod", "service"]
    assert calls[0][1]["body"].preconditions.uid == "pod-uid-1"


@pytest.mark.parametrize(
    ("start_succeeds", "error_match"),
    [
        (True, "is not ready"),
        (False, "container start failed"),
    ],
)
def test_docker_backend_cleans_up_sandbox_and_network_on_failure(monkeypatch, tmp_path, start_succeeds, error_match):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    created_container = None
    deleted_networks = []

    class FakeContainer:
        name = "yuxi-sandbox-sandbox-1"
        status = "running"
        labels = {"thread-id": "thread-1"}
        attrs = {"State": {"Status": "running"}}
        removed = False

        def reload(self):
            return None

        def stop(self, timeout):
            assert timeout == 10
            self.status = "exited"

        def remove(self, *, v, force):
            assert v is True
            assert force is True
            self.removed = True

    def run_container(_image, **_kwargs):
        nonlocal created_container
        if not start_succeeds:
            raise RuntimeError("container start failed")
        created_container = FakeContainer()
        return created_container

    backend = _docker_backend(module, tmp_path, run_container)
    (tmp_path / "shared/user-1/workspace").mkdir(parents=True)
    (tmp_path.parent / "skill-projections/user-1").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(backend, "_get_container", lambda _sandbox_id: created_container)
    monkeypatch.setattr(backend, "_ensure_network", backend._network_name)
    monkeypatch.setattr(backend, "_delete_network", deleted_networks.append)
    monkeypatch.setattr(module, "wait_for_sandbox_ready", lambda _url, timeout_seconds: False)

    with pytest.raises(RuntimeError, match=error_match):
        backend.create("sandbox-1", "thread-1", "user-1")

    assert deleted_networks == ["sandbox-1"]
    if start_succeeds:
        assert created_container is not None
        assert created_container.removed is True


def test_docker_backend_assigns_each_sandbox_a_distinct_network(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    backend = object.__new__(module.LocalContainerProvisionerBackend)
    backend._network_prefix = "yuxi-know-sandbox"

    first_network = backend._network_name("sandbox-1")
    second_network = backend._network_name("sandbox-2")

    assert first_network == "yuxi-know-sandbox-sandbox-1"
    assert second_network == "yuxi-know-sandbox-sandbox-2"
    assert first_network != second_network
    assert backend._is_on_expected_network(
        SimpleNamespace(attrs={"NetworkSettings": {"Networks": {first_network: {}}}}),
        "sandbox-1",
    )
    assert not backend._is_on_expected_network(
        SimpleNamespace(attrs={"NetworkSettings": {"Networks": {first_network: {}, second_network: {}}}}),
        "sandbox-1",
    )


def test_docker_backend_reconnects_provisioner_before_reusing_sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    monkeypatch.setitem(sys.modules, "docker.errors", SimpleNamespace(NotFound=RuntimeError))
    module = _load_module()
    connected = []

    class FakeNetwork:
        name = "yuxi-know-sandbox-sandbox-1"
        attrs = {
            "Labels": {"managed-by": "yuxi-sandbox-provisioner", "sandbox-id": "sandbox-1"},
            "Containers": {},
        }

        def reload(self):
            return None

        def connect(self, container, aliases):
            connected.append((container.id, aliases))

    class FakeContainer:
        name = "yuxi-sandbox-sandbox-1"
        status = "running"
        labels = {"thread-id": "thread-1", "uid": "user-1"}
        attrs = {
            "State": {"Status": "running"},
            "HostConfig": {
                "Memory": 4 * 1024**3,
                "MemorySwap": 4 * 1024**3,
                "NanoCpus": 2_000_000_000,
                "PidsLimit": 512,
                "Tmpfs": {"/home/gem": "rw,exec,mode=777,size=1024m"},
            },
        }

        def reload(self):
            return None

    backend = _docker_backend(module, tmp_path, lambda *_args, **_kwargs: pytest.fail("sandbox was recreated"))
    backend._client.networks = SimpleNamespace(get=lambda _name: FakeNetwork())
    backend._provisioner_container = SimpleNamespace(id="provisioner-id")
    monkeypatch.setattr(backend, "_get_container", lambda _sandbox_id: FakeContainer())
    monkeypatch.setattr(backend, "_is_expected_skills_mount", lambda _container, _uid: True)
    monkeypatch.setattr(backend, "_is_on_expected_network", lambda _container, _sandbox_id: True)
    monkeypatch.setattr(backend, "_has_expected_user_data_mounts", lambda _container, _uid, _workdir=None: True)
    monkeypatch.setattr(module, "wait_for_sandbox_ready", lambda _url, timeout_seconds: bool(connected))

    record = backend.create("sandbox-1", "thread-1", "user-1")

    assert record.sandbox_url == "http://yuxi-sandbox-sandbox-1:8080"
    assert connected == [("provisioner-id", ["sandbox-provisioner"])]


def test_docker_backend_does_not_remove_unowned_network(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    monkeypatch.setitem(sys.modules, "docker.errors", SimpleNamespace(NotFound=RuntimeError))
    module = _load_module()
    disconnected = []
    removed = []

    class FakeNetwork:
        name = "yuxi-know-sandbox-sandbox-1"
        attrs = {
            "Labels": {"managed-by": "operator", "sandbox-id": "sandbox-1"},
            "Containers": {"provisioner-id": {}},
        }

        def reload(self):
            return None

        def disconnect(self, container, force):
            disconnected.append((container.id, force))

        def remove(self):
            removed.append(True)

    backend = _docker_backend(module, tmp_path, lambda *_args, **_kwargs: None)
    backend._client.networks = SimpleNamespace(get=lambda _name: FakeNetwork())
    backend._provisioner_container = SimpleNamespace(id="provisioner-id")

    backend._delete_network("sandbox-1")

    assert disconnected == []
    assert removed == []


def test_kubernetes_inventory_includes_pod_without_service_and_fails_closed(
    monkeypatch,
):
    """迁移清理必须以 Pod 为权威事实，不能把 Service 丢失或枚举失败当成空集。"""
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    kubernetes_module = ModuleType("kubernetes")
    client_module = ModuleType("kubernetes.client")
    rest_module = ModuleType("kubernetes.client.rest")

    class ApiException(Exception):
        pass

    rest_module.ApiException = ApiException
    client_module.rest = rest_module
    kubernetes_module.client = client_module
    monkeypatch.setitem(sys.modules, "kubernetes", kubernetes_module)
    monkeypatch.setitem(sys.modules, "kubernetes.client", client_module)
    monkeypatch.setitem(sys.modules, "kubernetes.client.rest", rest_module)
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            labels={
                "app": "yuxi-sandbox",
                "sandbox-id": "orphan-1",
            },
            annotations={"workdir-path": "projects/11111111-1111-4111-8111-111111111111"},
            uid="pod-generation-1",
        ),
        status=SimpleNamespace(phase="Terminating"),
    )

    class FakeCoreApi:
        fail = False
        selectors = []

        def list_namespaced_pod(self, **kwargs):
            self.selectors.append(kwargs["label_selector"])
            if self.fail:
                raise ApiException("inventory unavailable")
            return SimpleNamespace(items=[pod])

    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._core_api = FakeCoreApi()
    backend._namespace = "yuxi"

    assert backend.list() == [
        module.SandboxRecord(
            sandbox_id="orphan-1",
            sandbox_url="",
            status="Terminating",
            generation="pod-generation-1",
            workdir_path="projects/11111111-1111-4111-8111-111111111111",
        )
    ]
    assert backend._core_api.selectors == ["app=yuxi-sandbox"]
    backend._core_api.fail = True
    with pytest.raises(ApiException, match="inventory unavailable"):
        backend.list()


def test_quiesce_waits_for_authoritative_inventory_and_blocks_new_creates(
    monkeypatch,
):
    """删除请求返回后仍须看到权威 inventory 归零，且窗口内不得创建新 runtime。"""
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    record = module.SandboxRecord(
        sandbox_id="sandbox-1",
        sandbox_url="",
        status="Terminating",
        generation="generation-1",
    )

    class FakeBackend:
        inventories = iter(([record], [record], []))

        def __init__(self):
            self.deletes = []

        def list(self):
            return next(self.inventories)

        def delete(self, sandbox_id, *, expected_generation=None):
            self.deletes.append((sandbox_id, expected_generation))

    backend = FakeBackend()
    gate = module.SandboxQuiescenceGate()
    monkeypatch.setattr(module, "backend_impl", backend)
    monkeypatch.setattr(module, "sandbox_quiescence_gate", gate)
    monkeypatch.setattr(module, "sandbox_operation_pins", module.SandboxOperationPins())
    monkeypatch.setattr(module.idle_reaper, "forget", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    response = module.quiesce_sandboxes(timeout_seconds=1)

    assert response == module.QuiesceSandboxesResponse(ok=True, deleted=1)
    assert backend.deletes == [
        ("sandbox-1", "generation-1"),
        ("sandbox-1", "generation-1"),
    ]
    with pytest.raises(module.HTTPException) as exc_info:
        module.create_sandbox(
            module.CreateSandboxRequest(
                sandbox_id="new-sandbox",
                thread_id="thread-1",
                uid="user-1",
            )
        )
    assert exc_info.value.status_code == 503


def test_quiescence_gate_waits_for_create_already_in_flight():
    """停机栅栏必须排空先进入的 create，避免判空后又出现新 generation。"""
    module = _load_module()
    gate = module.SandboxQuiescenceGate()
    gate.acquire_create()
    entered = threading.Event()
    finished = threading.Event()

    def begin_quiescence():
        entered.set()
        gate.begin()
        finished.set()

    thread = threading.Thread(target=begin_quiescence)
    thread.start()
    assert entered.wait(timeout=1)
    assert not finished.wait(timeout=0.05)
    gate.release_create()
    assert finished.wait(timeout=1)
    thread.join(timeout=1)

    with pytest.raises(RuntimeError, match="quiescing"):
        gate.acquire_create()
