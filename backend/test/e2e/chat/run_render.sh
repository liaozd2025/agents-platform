#!/usr/bin/env bash
# 主路由使用真实认证/API，性能负载在浏览器注入合成消息；服务、profile、数据均一次性。
set -euo pipefail
export KB_E2E_API_IMAGE="${1:?Provide API_IMAGE}"
export KB_E2E_PROVISIONER_IMAGE="${2:?Provide PROVISIONER_IMAGE}"
export KB_E2E_REPO_ROOT
KB_E2E_REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
export COMPOSE_PROJECT_NAME="chat-render-$(date +%s)-$$"
export KB_E2E_STATE_DIR
KB_E2E_STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/chat-render.XXXXXX")"
mkdir -p "$KB_E2E_STATE_DIR/threads" "$KB_E2E_STATE_DIR/projections"
export CLIENT_API_PORT="${CLIENT_API_PORT:-45601}" CLIENT_WEB_PORT="${CLIENT_WEB_PORT:-45603}" CLIENT_CDP_PORT="${CLIENT_CDP_PORT:-45604}"
export CLIENT_EVIDENCE_DIR="${CLIENT_EVIDENCE_DIR:-$KB_E2E_REPO_ROOT/tmp/chat-render-fix}"
mkdir -p "$CLIENT_EVIDENCE_DIR"
compose=(docker compose --env-file /dev/null -p "$COMPOSE_PROJECT_NAME" -f "$KB_E2E_REPO_ROOT/backend/test/e2e/knowledge/compose.yaml" -f "$KB_E2E_REPO_ROOT/backend/test/e2e/clients/compose.yaml")
children=()
cleanup() {
    result=$?
    if (( result != 0 )); then "${compose[@]}" logs --tail 15 > "$CLIENT_EVIDENCE_DIR/services.log"; fi
    for pid in ${children[@]+"${children[@]}"}; do kill "$pid" 2>/dev/null || true; done
    for pid in ${children[@]+"${children[@]}"}; do wait "$pid" 2>/dev/null || true; done
    "${compose[@]}" down --volumes --remove-orphans
    # Linux 宿主用户必须能清理容器创建的 0700 目录。
    docker run --rm --network none --user 0:0 --entrypoint chown -v "$KB_E2E_STATE_DIR:/state" "$KB_E2E_API_IMAGE" -R "$(id -u):$(id -g)" /state
    rm -rf -- "$KB_E2E_STATE_DIR"
}
trap cleanup EXIT
"${compose[@]}" pull --policy missing postgres redis minio
"${compose[@]}" up -d --wait --wait-timeout 120 api worker replay redis minio sandbox-provisioner
"${compose[@]}" exec -T api uv run --no-sync python test/e2e/clients/fixture.py setup
"${compose[@]}" exec -T api chown "$(id -u):$(id -g)" /state/client.json
(cd "${CHAT_WEB_ROOT:-$KB_E2E_REPO_ROOT/web}" && VITE_API_URL="http://127.0.0.1:$CLIENT_API_PORT" exec node -e '
(async () => {
const { createServer } = await import("vite");
const server = await createServer({ cacheDir: process.env.KB_E2E_STATE_DIR + "/vite-cache", server: { host: "127.0.0.1", port: Number(process.env.CLIENT_WEB_PORT), strictPort: true } });
await server.listen();
})().catch(error => { console.error(error); process.exit(1); });
' ) > "$CLIENT_EVIDENCE_DIR/vite.log" 2>&1 &
children+=("$!")
"${CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}" --headless=new --enable-precise-memory-info --disable-background-networking --disable-gpu --no-first-run --no-default-browser-check --user-data-dir="$KB_E2E_STATE_DIR/chrome" --remote-debugging-port="$CLIENT_CDP_PORT" about:blank > "$KB_E2E_STATE_DIR/chrome.log" 2>&1 &
children+=("$!")
node "$KB_E2E_REPO_ROOT/backend/test/e2e/chat/browser.mjs"
