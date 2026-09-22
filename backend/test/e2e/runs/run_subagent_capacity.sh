#!/usr/bin/env bash
# 使用一次性环境运行子任务容量 E2E，成功或失败都删除本次容器、网络和数据。
set -euo pipefail
export KB_E2E_API_IMAGE="${1:?Usage: bash run_subagent_capacity.sh API_IMAGE PROVISIONER_IMAGE}"
export KB_E2E_PROVISIONER_IMAGE="${2:?Provide the dependency-matched local provisioner image}"
export KB_E2E_REPO_ROOT
KB_E2E_REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
export COMPOSE_PROJECT_NAME="subagent-capacity-$(date +%s)-$$"
export KB_E2E_STATE_DIR
KB_E2E_STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/subagent-capacity.XXXXXX")"
mkdir -p "$KB_E2E_STATE_DIR/threads" "$KB_E2E_STATE_DIR/projections"
compose=(docker compose --env-file /dev/null -p "$COMPOSE_PROJECT_NAME" -f "$KB_E2E_REPO_ROOT/backend/test/e2e/knowledge/compose.yaml" -f "$KB_E2E_REPO_ROOT/backend/test/e2e/runs/compose.capacity.yaml")
cleanup() {
    # 失败时保留诊断输出，销毁范围限定为本次唯一 project。
    if (( $? != 0 )); then "${compose[@]}" logs --tail 300; fi
    "${compose[@]}" exec -T worker python -c 'from pathlib import Path; print("worker cgroup memory current/peak:", Path("/sys/fs/cgroup/memory.current").read_text().strip(), Path("/sys/fs/cgroup/memory.peak").read_text().strip())' || true
  "${compose[@]}" down --volumes --remove-orphans
  # provisioner 动态创建的沙箱不属于 Compose，只清理本次唯一前缀。
  docker ps -aq --filter "name=^${COMPOSE_PROJECT_NAME}-" | while IFS= read -r id; do docker rm -f "$id"; done
  docker network ls -q --filter "name=^${COMPOSE_PROJECT_NAME}-" | while IFS= read -r id; do docker network rm "$id"; done
    docker run --rm --network none --user 0:0 --entrypoint chown -v "$KB_E2E_STATE_DIR:/state" "$KB_E2E_API_IMAGE" -R "$(id -u):$(id -g)" /state
    rm -rf -- "$KB_E2E_STATE_DIR"
}
trap cleanup EXIT
"${compose[@]}" pull --policy missing postgres redis minio
# 迁移器必须以 root 完成身份迁移；其日志也在随后移交的状态目录中。
"${compose[@]}" run --rm migrator
# 与 shipping API/worker 的 UID 一致，避免 Linux 上 root 创建的私有目录阻断 Sandbox。
docker run --rm --network none --user 0:0 --entrypoint chown -v "$KB_E2E_STATE_DIR:/state" "$KB_E2E_API_IMAGE" -R 1000:1000 /state
"${compose[@]}" up -d --no-deps --wait --wait-timeout 120 api worker replay redis minio sandbox-provisioner
"${compose[@]}" exec -T api uv run --no-sync pytest --confcutdir=test/e2e/runs test/e2e/runs/test_subagent_capacity.py -q -s
