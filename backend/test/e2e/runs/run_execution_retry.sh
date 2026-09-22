#!/usr/bin/env bash
# 在一次性环境中精确停止/重启 worker；成败均销毁本次环境。
set -euo pipefail
export KB_E2E_API_IMAGE="${1:?Usage: bash run_execution_retry.sh API_IMAGE PROVISIONER_IMAGE}"
export KB_E2E_PROVISIONER_IMAGE="${2:?Provide the dependency-matched local provisioner image}"
export KB_E2E_REPO_ROOT
KB_E2E_REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
export COMPOSE_PROJECT_NAME="run-retry-$(date +%s)-$$"
export KB_E2E_STATE_DIR
KB_E2E_STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/run-retry.XXXXXX")"
mkdir -p "$KB_E2E_STATE_DIR/threads" "$KB_E2E_STATE_DIR/projections"
compose=(docker compose --env-file /dev/null -p "$COMPOSE_PROJECT_NAME" -f "$KB_E2E_REPO_ROOT/backend/test/e2e/knowledge/compose.yaml" -f "$KB_E2E_REPO_ROOT/backend/test/e2e/runs/compose.retry.yaml")
cleanup() {
    # 只销毁本次唯一 project 及其临时目录。
    if (( $? != 0 )); then "${compose[@]}" logs --tail 30; fi
    "${compose[@]}" down --volumes --remove-orphans
    docker run --rm --network none --user 0:0 --entrypoint chown -v "$KB_E2E_STATE_DIR:/state" "$KB_E2E_API_IMAGE" -R "$(id -u):$(id -g)" /state
    rm -rf -- "$KB_E2E_STATE_DIR"
}
trap cleanup EXIT
"${compose[@]}" pull --policy missing postgres redis minio
"${compose[@]}" up -d --wait --wait-timeout 120 api worker replay redis minio sandbox-provisioner
"${compose[@]}" exec -T api uv run --no-sync pytest --confcutdir=test/e2e/runs test/e2e/runs/test_execution_retry.py -q &
test_pid=$!
for ((i=0; i<600; i++)); do
    if [[ -f "$KB_E2E_STATE_DIR/stop-worker" ]]; then break; fi
    if ! kill -0 "$test_pid" 2>/dev/null; then wait "$test_pid"; exit 1; fi
    sleep 0.1
done
[[ -f "$KB_E2E_STATE_DIR/stop-worker" ]]
"${compose[@]}" stop -t 30 worker
touch "$KB_E2E_STATE_DIR/worker-stopped"
for ((i=0; i<300; i++)); do
    if [[ -f "$KB_E2E_STATE_DIR/pending-created" ]]; then break; fi
    if ! kill -0 "$test_pid" 2>/dev/null; then wait "$test_pid"; exit 1; fi
    sleep 0.1
done
[[ -f "$KB_E2E_STATE_DIR/pending-created" ]]
"${compose[@]}" up -d --no-deps worker
touch "$KB_E2E_STATE_DIR/worker-restarted"
wait "$test_pid"
