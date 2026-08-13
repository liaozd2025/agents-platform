# OA iframe 接入说明

Yuxi 提供 `/embed` 和 `/embed/{thread_id}` 两个无外壳路由。OA 只需要嵌入 iframe，并按本文的 `postMessage` 协议下发 Yuxi bearer token。

## 部署前提

- OA 与 Yuxi 应部署在同一主域的不同子域，例如 `oa.corp.com` 与 `ai.corp.com`。跨主域或裸 IP 部署可能触发浏览器存储分区，需要重新评估免登录体验。
- `YUXI_EMBED_ALLOWED_ORIGINS` 只填 OA 的精确 origin，不带路径。多个 origin 用空格分隔。留空时 CSP 仅允许同源嵌入。
- `YUXI_CORS_ORIGINS` 同步加入 OA origin，供 OA 交换一次性登录 code。
- Web 镜像会在构建时将同一白名单写入前端，Nginx 在启动时将它写入 `frame-ancestors`。修改 origin 后需重新构建并启动 Web 服务，不需要改代码。

OIDC 配置项：

```dotenv
OIDC_ENABLED=true
OIDC_ISSUER_URL=https://oa.corp.com/oidc
OIDC_CLIENT_ID=<OA 签发的 client_id>
OIDC_CLIENT_SECRET=<OA 签发的 client_secret>
OIDC_REDIRECT_URI=https://ai.corp.com/api/auth/oidc/callback
# 仅显式端点模式必填；discovery 模式自动获取
# OIDC_JWKS_URI=https://oa.corp.com/oidc/jwks
OIDC_DEPARTMENT_CLAIM=department
YUXI_EMBED_ALLOWED_ORIGINS=https://oa.corp.com
YUXI_CORS_ORIGINS=https://oa.corp.com
```

`id_token` 必须包含 `exp`、`iat`、`iss`、`aud`、`sub` 和 `nonce`；多受众令牌还必须用 `azp` 指向本系统 client。Provider discovery 必须给出匹配配置的 `issuer` 和 `jwks_uri`，所有 OIDC 端点必须使用 HTTPS；仅 `YUXI_ENV=development` 时允许本机 HTTP Provider。部门从 `OIDC_DEPARTMENT_CLAIM` 指定的 claim 读取；缺失时用户会落入 `OIDC_DEFAULT_DEPARTMENT`，同时后端记录 warning。

## iframe 与消息协议

```html
<iframe
  src="https://ai.corp.com/embed"
  sandbox="allow-scripts allow-same-origin allow-downloads allow-popups allow-popups-to-escape-sandbox"
></iframe>
```

OA 页面只接受来自 Yuxi origin 且 `event.source === iframe.contentWindow` 的消息；向 Yuxi 发送时也必须使用精确 `targetOrigin`，不得使用 `*`。

```js
const yuxiOrigin = 'https://ai.corp.com'
const frame = document.querySelector('#yuxi-frame')

window.addEventListener('message', (event) => {
  if (event.origin !== yuxiOrigin || event.source !== frame.contentWindow) return

  if (event.data?.type === 'yuxi:ready') {
    frame.contentWindow.postMessage({ type: 'oa:token', token: yuxiBearer }, yuxiOrigin)
  }
  if (event.data?.type === 'yuxi:auth-required') {
    refreshYuxiBearer()
  }
  if (event.data?.type === 'yuxi:expand') {
    location.assign(`${yuxiOrigin}/agent/${encodeURIComponent(event.data.threadId)}`)
  }
})
```

完整时序为：iframe 先发 `yuxi:ready`，OA 再发 `oa:token`。token 失效时 Yuxi 发 `yuxi:auth-required`，OA 重新走 OIDC 并下发新 token。窄容器点击产物时 Yuxi 发 `yuxi:expand` 和当前 `threadId`。

## sandbox 边界

- `allow-scripts`：Vue 应用运行必需。
- `allow-same-origin`：保留 Yuxi origin 和 localStorage；本方案要求 OA 与 Yuxi 跨源，iframe 无法访问 `parent.document` 去移除自身 sandbox。
- `allow-downloads`：产物下载必需。
- `allow-popups allow-popups-to-escape-sandbox`：对话中外部来源链接必需。

不需要 `allow-forms` 或 `allow-modals`：上传使用 fetch/FormData，确认框使用页面组件，没有原生表单提交或浏览器 modal。

## 本地模拟验收

模拟程序不包含真实 OA 地址、账号或凭据。先启动 Provider 和 OA 静态页：

```bash
docker run --rm -p 9001:9001 \
  -e MOCK_OIDC_ISSUER=http://host.docker.internal:9001 \
  -e MOCK_OIDC_BROWSER_ORIGIN=http://localhost:9001 \
  -v "$PWD/backend/test/e2e/fixtures:/fixtures:ro" \
  yuxi-api:0.7.1 \
  uv run --no-sync uvicorn oa_oidc_mock:app --app-dir /fixtures --host 0.0.0.0 --port 9001

docker run --rm -p 4173:4173 \
  -v "$PWD/backend/test/e2e/fixtures:/fixtures:ro" \
  -w /fixtures yuxi-api:0.7.1 \
  python -m http.server 4173 --bind 0.0.0.0
```

本地 Yuxi 使用以下模拟配置后重建启动：

```dotenv
OIDC_ENABLED=true
OIDC_ISSUER_URL=http://host.docker.internal:9001
OIDC_CLIENT_ID=oa-s0-local-client
OIDC_CLIENT_SECRET=oa-s0-local-secret
OIDC_REDIRECT_URI=http://localhost:5050/api/auth/oidc/callback
OIDC_DEPARTMENT_CLAIM=department
YUXI_EMBED_ALLOWED_ORIGINS=http://localhost:4173
YUXI_CORS_ORIGINS=http://localhost:4173
```

打开 `http://localhost:4173/oa_embed_mock.html`，依次验收：

1. 400px 下 iframe 只显示对话界面，不出现 Yuxi 登录框。
2. 点击“OIDC 免登录”，返回后日志出现 `yuxi:ready` 和 `oa:token`。
3. 发起一轮对话，确认流式输出。
4. 生成并下载一个产物；400px 点产物时日志出现带 `threadId` 的 `yuxi:expand`。
5. 点击“模拟 token 过期”，日志出现 `yuxi:auth-required`，重新授权后继续对话。
6. 切换到 1920px，重复对话、产物预览与下载。

负向验证：从未加入 `YUXI_EMBED_ALLOWED_ORIGINS` 的端口打开同一 OA 页，浏览器应按 CSP 拒绝 iframe；从该页伪造 `oa:token` 也不会被 Yuxi 接受。OIDC 错误签名、nonce 不匹配、一次性 code 重放和跨副本消费由后端自动化测试覆盖。
