# OA iframe 接入说明

Yuxi 提供 `/embed` 和 `/embed/{thread_id}` 两个无外壳路由。九典 OA 使用现有自定义 token 登录，不需要新建 OIDC 平台或修改 OA 后端。

## 认证流程

1. OA 页面加载 Yuxi iframe。
2. Yuxi 发送 `yuxi:ready`。
3. OA 将当前登录 token 通过 `oa:token` 发给 iframe。
4. Yuxi 后端从 OA 双 JWT token 的两段载荷中取出一致账号，该值只作为 OA 查询参数。
5. Yuxi 携带完整 OA token 请求 `GetUserInfo`，以 OA 接口返回的账号、公司和在职状态作为最终身份依据。
6. Yuxi 以 `companyCode + account` 匹配或创建本地用户，再签发自己的 bearer token。

OA token 只在本次交换中使用，不写入 URL、日志、数据库或 localStorage。iframe 最终保存和使用的是 Yuxi token。

## Yuxi 部署配置

OA 与 Yuxi 建议部署在同一主域的不同子域。`YUXI_EMBED_ALLOWED_ORIGINS` 只填 OA 页面的精确 origin，不带路径或尾部 `/`。

```dotenv
OIDC_ENABLED=false

OA_SSO_ENABLED=true
OA_SSO_USERINFO_URL=https://hnjiudian.cn/oa-api/User/GetUserInfo
OA_SSO_COMPANY_CODE=ZD

YUXI_EMBED_ALLOWED_ORIGINS=https://hnjiudian.cn
```

OA 父页只通过 `postMessage` 与 iframe 通信，iframe 再同源请求 Yuxi API，因此该链路不需要额外开放 CORS。修改 origin 后必须重建 Web 镜像，使前端白名单和 Nginx `frame-ancestors` 同步生效。

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
docker compose --env-file .env.prod -f docker-compose.prod.yml ps
```

## OA 页面接入

```html
<div id="yuxi-shell" data-mode="fixed">
  <iframe
    id="yuxi-frame"
    src="https://ai.example.com/embed"
    title="Yuxi 智能助手"
    sandbox="allow-scripts allow-same-origin allow-downloads allow-popups allow-popups-to-escape-sandbox"
  ></iframe>
</div>
```

OA 必须校验 Yuxi 消息的 `event.origin` 和 `event.source`，发送时也必须使用精确 `targetOrigin`，不得使用 `*`。

```js
const yuxiOrigin = 'https://ai.example.com'
const frame = document.querySelector('#yuxi-frame')
const shell = document.querySelector('#yuxi-shell')
const modes = new Set(['fixed', 'floating', 'fullscreen'])

function sendOAToken() {
  const token = getCurrentOAToken() // 复用 OA 现有的登录 token 取值方式
  frame.contentWindow.postMessage({ type: 'oa:token', token }, yuxiOrigin)
}

function applyMode(mode) {
  if (!modes.has(mode)) return
  shell.hidden = false
  shell.dataset.mode = mode // OA 根据该值设置固定、浮窗或全屏布局
  frame.contentWindow.postMessage({ type: 'oa:mode-changed', mode }, yuxiOrigin)
}

function openYuxi() {
  sendOAToken()
  applyMode('fixed') // 每次打开默认固定模式，不重载 iframe
}

window.addEventListener('message', (event) => {
  if (event.origin !== yuxiOrigin || event.source !== frame.contentWindow) return

  if (event.data?.type === 'yuxi:ready') {
    sendOAToken()
    applyMode('fixed')
  }

  if (event.data?.type === 'yuxi:auth-required') {
    refreshOALogin().then(sendOAToken)
  }

  if (event.data?.type === 'yuxi:mode-request') {
    applyMode(event.data.mode)
  }

  if (event.data?.type === 'yuxi:close-request') {
    shell.hidden = true
  }
})
```

`getCurrentOAToken()` 和 `refreshOALogin()` 由 OA 按现有登录机制提供。OA 的打开按钮调用 `openYuxi()`；关闭只隐藏容器，不销毁或重载 iframe。不要把 token 放入 URL、DOM 属性或日志。

## 显示模式协议

Yuxi 只提出显示请求，OA 父页负责 iframe 容器布局并回传实际结果：

| 方向 | 消息 | 载荷 |
|---|---|---|
| Yuxi → OA | `yuxi:mode-request` | `{ mode: 'fixed' \| 'floating' \| 'fullscreen', threadId? }` |
| OA → Yuxi | `oa:mode-changed` | `{ mode: 'fixed' \| 'floating' \| 'fullscreen' }` |
| Yuxi → OA | `yuxi:close-request` | `{ threadId? }` |

切换模式不得修改 iframe 的 `/embed` 路由、清空会话或重新认证。独立站 `/agent` 不显示这些控件，也不参与该协议。

## OA 用户映射

Yuxi 仅保存完成登录和权限所需的最小字段：

| OA 字段 | Yuxi 用途 |
|---|---|
| `companyCode + account` | 稳定身份键 `oa:<companyCode>:<account>` |
| `fullName` / `userName` | 显示用户名 |
| `userStateCode` | 仅 `service` 允许登录 |
| `userJobInformationDtos` | 按 `pagingSort` 最小值选择主任职 |
| `appointmentDepartmentName` | Yuxi 部门名称 |

手机号、照片、岗位和职级不在 S0 保存。OA 新用户统一创建为 `user`，不会根据岗位、职级或 OA 管理员字段自动提权。

## 接口边界

iframe 通过下列 Yuxi 接口交换登录态：

```http
POST /api/auth/oa/exchange-token
Content-Type: application/json

{"token":"<OA_TOKEN>"}
```

Yuxi 后端只请求配置中的固定 OA URL，不接受客户端传入上游 URL：

```http
GET /oa-api/User/GetUserInfo?Account=<token中的账号>
Authorization: Bearer <OA_TOKEN>
Accept: application/json
```

成功必须同时满足：HTTP 200、`status == 1`、返回账号与 token 两段一致、`companyCode` 与配置一致、`userStateCode == "service"`。

## 上线验收

1. 固定、浮窗和全屏模式下 iframe 均不出现 Yuxi 登录框。
2. OA 有效 token 能交换 Yuxi token，篡改、过期或非在职 token 被拒绝。
3. 返回账号或公司不匹配时拒绝登录。
4. 发起一轮对话并看到流式输出。
5. 依次切换固定、浮窗、全屏、固定，父页收到 `yuxi:mode-request` 并回传 `oa:mode-changed`，iframe 路由和会话不变。
6. Yuxi token 失效时 OA 收到 `yuxi:auth-required`，刷新 OA token 后可继续对话。
7. 关闭后 iframe 仅隐藏；再次打开默认固定模式，最近会话和上下文仍在。
8. 非白名单 origin 无法嵌入 Yuxi，伪造 `oa:token` 和 `oa:mode-changed` 不被接受。

## 本地模拟验收

现有测试 fixture 同时提供模拟 OA token、用户接口和父页面。先以开发配置启动 Yuxi：

```dotenv
OIDC_ENABLED=false
OA_SSO_ENABLED=true
OA_SSO_USERINFO_URL=http://host.docker.internal:9101/oa-api/User/GetUserInfo
OA_SSO_COMPANY_CODE=TEST
YUXI_EMBED_ALLOWED_ORIGINS=http://localhost:4173
```

再分别启动模拟身份服务和静态父页面：

```bash
docker run --rm --name oa-sso-mock -p 9101:9101 \
  -v "$PWD/backend/test:/app/test:ro" \
  yuxi-api:0.7.1 uv run --no-sync --no-dev \
  uvicorn test.e2e.fixtures.oa_oidc_mock:app --host 0.0.0.0 --port 9101

docker run --rm --name oa-embed-mock -p 4173:4173 \
  -v "$PWD/backend/test/e2e/fixtures:/site:ro" \
  yuxi-api:0.7.1 python -m http.server 4173 --directory /site
```

打开 `http://localhost:4173/oa_embed_mock.html`，按固定、浮窗、全屏、关闭后重开、token 过期的顺序验收。模拟服务仅用于本地开发，生产不得启用。

OIDC 实现仍保留为其他身份系统的可选能力，九典 OA 生产接入使用本文的自定义 SSO 流程，并保持 `OIDC_ENABLED=false`。
