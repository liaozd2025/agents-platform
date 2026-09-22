#!/usr/bin/env bash
# 使用一次性环境运行模型失败 E2E，成功或失败都删除本次容器、网络和数据。
set -euo pipefail
export KB_E2E_API_IMAGE="${1:?Usage: bash run_model_failure.sh API_IMAGE PROVISIONER_IMAGE}"
export KB_E2E_PROVISIONER_IMAGE="${2:?Provide the dependency-matched local provisioner image}"
export KB_E2E_REPO_ROOT
KB_E2E_REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
export COMPOSE_PROJECT_NAME="model-failure-$(date +%s)-$$"
export KB_E2E_STATE_DIR
KB_E2E_STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/model-failure.XXXXXX")"
mkdir -p "$KB_E2E_STATE_DIR/threads" "$KB_E2E_STATE_DIR/projections"
compose=(docker compose --env-file /dev/null -p "$COMPOSE_PROJECT_NAME" -f "$KB_E2E_REPO_ROOT/backend/test/e2e/knowledge/compose.yaml" -f "$KB_E2E_REPO_ROOT/backend/test/e2e/runs/compose.model-failure.yaml")
cleanup() {
    # 失败时保留诊断输出，销毁范围限定为本次唯一 project。
    if (( $? != 0 )); then "${compose[@]}" logs --tail 30; fi
    "${compose[@]}" down --volumes --remove-orphans
    docker run --rm --network none --user 0:0 --entrypoint chown -v "$KB_E2E_STATE_DIR:/state" "$KB_E2E_API_IMAGE" -R "$(id -u):$(id -g)" /state
    rm -rf -- "$KB_E2E_STATE_DIR"
}
trap cleanup EXIT
"${compose[@]}" pull --policy missing postgres redis minio
"${compose[@]}" up -d --wait --wait-timeout 120 api worker replay redis minio sandbox-provisioner
"${compose[@]}" exec -T api uv run --no-sync pytest --confcutdir=test/e2e/runs test/e2e/runs/test_model_failure.py -q
