"""将既有部署资源预算转换为 runner 配置，随后启动指定进程。"""

import math
import os
import sys


def configure_resources() -> None:
    """新变量优先；只转换仍有真实部署消费者的旧预算变量。"""
    for name, legacy, factor, default in (
        ("SANDBOX_CONTAINER_MEMORY_BYTES", "SANDBOX_MEMORY_MB", 1048576, 4294967296),
        ("SANDBOX_CONTAINER_TMP_BYTES", "SANDBOX_TMPFS_MB", 1048576, 1073741824),
        ("SANDBOX_CONTAINER_PIDS_LIMIT", "SANDBOX_DOCKER_PIDS_LIMIT", 1, 512),
        ("SANDBOX_CONTAINER_MAX_CONTAINERS", "SANDBOX_MAX_INSTANCES", 1, 6),
        (
            "SANDBOX_CONTAINER_MAX_CONTAINERS_PER_USER",
            "SANDBOX_MAX_INSTANCES_PER_USER",
            1,
            4,
        ),
    ):
        raw = os.environ.get(name) or ""
        old = os.environ.get(legacy) or ""
        try:
            value = int(raw) if raw else int(old) * factor if old else default
        except ValueError as exc:
            raise ValueError(f"{name} / {legacy} 必须是正整数") from exc
        if value <= 0:
            raise ValueError(f"{name} / {legacy} 必须是正整数")
        os.environ[name] = str(value)
    raw = (
        os.environ.get("SANDBOX_CONTAINER_CPUS")
        or os.environ.get("SANDBOX_CPUS")
        or "2"
    )
    cpus = float(raw)
    if not math.isfinite(cpus) or cpus < 0.01:
        raise ValueError(
            "SANDBOX_CONTAINER_CPUS / SANDBOX_CPUS 必须是至少 0.01 的有限数"
        )
    os.environ["SANDBOX_CONTAINER_CPUS"] = str(cpus)
    if int(os.environ["SANDBOX_CONTAINER_MEMORY_BYTES"]) < 6 * 1048576:
        raise ValueError("SANDBOX_CONTAINER_MEMORY_BYTES 必须至少为 6 MiB")
    if int(os.environ["SANDBOX_CONTAINER_TMP_BYTES"]) > int(
        os.environ["SANDBOX_CONTAINER_MEMORY_BYTES"]
    ):
        raise ValueError("SANDBOX_CONTAINER_TMP_BYTES 不能超过容器内存预算")
    if int(os.environ["SANDBOX_CONTAINER_MAX_CONTAINERS_PER_USER"]) > int(
        os.environ["SANDBOX_CONTAINER_MAX_CONTAINERS"]
    ):
        raise ValueError("每用户容器上限不能超过部署总上限")


if __name__ == "__main__":
    configure_resources()
    if len(sys.argv) < 2:
        raise SystemExit("缺少 runner 启动命令")
    os.execvp(sys.argv[1], sys.argv[1:])
