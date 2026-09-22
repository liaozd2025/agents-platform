#!/usr/bin/env bash
# 只启动一次性 PostgreSQL/Redis，双独立进程验证恢复扫描。
set -euo pipefail
export KB_E2E_API_IMAGE="${1:?Provide the local API dependency image}"
export KB_E2E_PROVISIONER_IMAGE="$KB_E2E_API_IMAGE"
export KB_E2E_REPO_ROOT
KB_E2E_REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
export COMPOSE_PROJECT_NAME="recovery-scan-$(date +%s)-$$"
export KB_E2E_STATE_DIR
KB_E2E_STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/recovery-scan.XXXXXX")"
compose=(docker compose --env-file /dev/null -p "$COMPOSE_PROJECT_NAME" -f "$KB_E2E_REPO_ROOT/backend/test/e2e/knowledge/compose.yaml" -f "$KB_E2E_REPO_ROOT/backend/test/e2e/runs/compose.recovery-scan.yaml")
cleanup() {
  if (( $? != 0 )); then "${compose[@]}" logs --tail 30; fi
  "${compose[@]}" down --volumes --remove-orphans
  docker run --rm --network none --user 0:0 --entrypoint chown -v "$KB_E2E_STATE_DIR:/state" "$KB_E2E_API_IMAGE" -R "$(id -u):$(id -g)" /state
  rm -rf -- "$KB_E2E_STATE_DIR"
}
trap cleanup EXIT
"${compose[@]}" pull --policy missing postgres redis
"${compose[@]}" up -d --wait postgres redis
"${compose[@]}" run --rm migrator
"${compose[@]}" run --rm --no-deps --entrypoint uv api run --no-sync pytest --confcutdir=test/integration/services test/integration/services/test_agent_request_queue_concurrency.py -q
"${compose[@]}" run --rm --no-deps --entrypoint uv api run --no-sync pytest --confcutdir=test/integration/services test/integration/services/test_recovery_scan.py -q -s
