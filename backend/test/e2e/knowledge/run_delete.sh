#!/usr/bin/env bash
# 一次性真实 HTTP/PG/Milvus/BM25/MinIO/Neo4j，故障与样本仅限该 project。
set -euo pipefail
export KB_E2E_API_IMAGE="${1:?Provide API_IMAGE}" KB_E2E_PROVISIONER_IMAGE="${2:?Provide PROVISIONER_IMAGE}"
export KB_E2E_REPO_ROOT
KB_E2E_REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
export COMPOSE_PROJECT_NAME="knowledge-delete-$(date +%s)-$$"
export KB_E2E_STATE_DIR
KB_E2E_STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/knowledge-delete.XXXXXX")"
mkdir -p "$KB_E2E_STATE_DIR/threads" "$KB_E2E_STATE_DIR/projections"
compose=(docker compose --env-file /dev/null -p "$COMPOSE_PROJECT_NAME" -f "$KB_E2E_REPO_ROOT/backend/test/e2e/knowledge/compose.yaml" -f "$KB_E2E_REPO_ROOT/backend/test/e2e/knowledge/compose.delete.yaml")
cleanup() {
  result=$?
  if (( result != 0 )); then "${compose[@]}" logs --tail 20; fi
  "${compose[@]}" down --volumes --remove-orphans
  docker run --rm --network none --user 0:0 --entrypoint chown -v "$KB_E2E_STATE_DIR:/state" "$KB_E2E_API_IMAGE" -R "$(id -u):$(id -g)" /state
  rm -rf -- "$KB_E2E_STATE_DIR"
}
trap cleanup EXIT
"${compose[@]}" pull --policy missing postgres redis minio etcd milvus graph
"${compose[@]}" up -d --wait --wait-timeout 180 postgres redis minio etcd milvus graph sandbox-provisioner api worker
"${compose[@]}" exec -T api uv run --no-sync pytest test/integration/knowledge/test_delete_consistency.py --confcutdir=test/integration/knowledge -q -s
"${compose[@]}" exec -T api uv run --no-sync pytest test/integration/graphs/test_milvus_graph_delete.py --confcutdir=test/integration/graphs -q
