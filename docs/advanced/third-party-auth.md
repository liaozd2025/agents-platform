# 接入 OIDC 登录

Yuxi 可以通过 OpenID Connect（OIDC）接入企业身份提供商。功能默认关闭；开启前，需要在身份提供商中注册客户端，并准备与用户实际访问地址完全一致的回调地址。

## 1. 注册 OIDC 客户端

记录以下信息：

- Client ID；
- Client Secret；
- Issuer URL。

把下面的后端回调地址注册为允许的 Redirect URI。生产环境请替换为实际域名并使用 HTTPS：

```text
https://<your-yuxi-host>/api/auth/oidc/callback
```

本机开发可以使用 Vite 代理后的 `http://localhost:5173/api/auth/oidc/callback`，前提是身份提供商允许该地址。Yuxi 位于反向代理后时，回调地址必须是用户访问的外部地址，而不是容器内部地址。

## 2. 配置 Yuxi

在 `.env` 或生产环境使用的 env file 中设置：

```bash
OIDC_ENABLED=true
OIDC_PROVIDER_NAME=企业登录
OIDC_ISSUER_URL=https://auth.example.com
OIDC_CLIENT_ID=<your-client-id>
OIDC_CLIENT_SECRET=<your-client-secret>
OIDC_REDIRECT_URI=https://<your-yuxi-host>/api/auth/oidc/callback
```

常用可选项：

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `OIDC_SCOPES` | `openid profile email` | 请求的 scope |
| `OIDC_AUTO_CREATE_USER` | `true` | 找不到本地账号时是否创建用户 |
| `OIDC_USERNAME_CLAIM` | `preferred_username` | 登录标识字段 |
| `OIDC_EMAIL_CLAIM` | `email` | 邮箱字段 |
| `OIDC_NAME_CLAIM` | `name` | 展示名称字段 |
| `OIDC_FORCE_PROMPT_LOGIN` | `true` | 是否在授权请求中加入 `prompt=login` |
| `OIDC_USE_RAW_USERNAME` | `false` | 是否用 OIDC 用户名匹配已有 Yuxi `uid` |
| `OIDC_FETCH_DEPARTMENT_INFO` | `false` | 是否读取组织节点 claim |
| `OIDC_DEPARTMENT_CLAIM` | `department` | 组织节点名称字段 |

自动创建的用户固定获得内置 `user` 角色，后续由有权限的管理员调整。系统没有 `OIDC_DEFAULT_ROLE` 或自动创建部门的配置。

### Discovery 与显式端点

未设置 `OIDC_AUTHORIZATION_ENDPOINT` 时，Yuxi 根据 `OIDC_ISSUER_URL` 请求 `/.well-known/openid-configuration`，读取授权、Token、UserInfo、JWKS 和可选登出端点。

只要设置了 `OIDC_AUTHORIZATION_ENDPOINT`，显式端点即成为权威配置，并且必须同时提供：

- `OIDC_TOKEN_ENDPOINT`；
- `OIDC_USERINFO_ENDPOINT`；
- `OIDC_JWKS_URI`。

`OIDC_END_SESSION_ENDPOINT` 可选。系统会校验 `id_token` 的签名、`iss`、`aud`、`exp`、`iat`、`sub` 和 `nonce`；Discovery 返回的 Issuer 必须与配置完全一致。OIDC 端点必须使用 HTTPS，仅开发环境允许 localhost HTTP。

`OIDC_CLIENT_SECRET` 和其他凭据只能放在受保护的运行环境中，不要提交到仓库或打印到日志。

## 3. 重建 API 容器

OIDC 配置在 API 启动时读取。修改环境变量后需要重新创建 API 容器：

```bash
docker compose up -d --force-recreate api
```

生产环境示例：

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --force-recreate api
```

## 登录流程

1. 前端向 `/api/auth/oidc/login-url` 请求授权地址。
2. 身份提供商登录后回调 `/api/auth/oidc/callback`。
3. API 校验一次性 `state`，用授权码换取 Token，校验 `id_token` 并读取 UserInfo。
4. API 把一次性登录 code 交给前端 `/auth/oidc/callback` 页面。
5. 前端调用 `/api/auth/oidc/exchange-code` 换取 Yuxi 登录态。

## 绑定已有账号

设置 `OIDC_USE_RAW_USERNAME=true` 后，Yuxi 会用 OIDC 返回的用户名匹配已有 `uid`。首次成功匹配时，系统创建一条已删除状态的占位用户，保存 OIDC `sub` 与目标用户的绑定关系；占位记录不能用于登录。

占位用户的 `uid` 格式为 `oidc:{sub}:{target_user_id}`。如果同一个 `sub` 已绑定到其他用户，登录会被拒绝。启用此模式前，应确认身份提供商中的用户名稳定且唯一，并提前创建需要绑定的账号。

## 映射组织节点

设置 `OIDC_FETCH_DEPARTMENT_INFO=true` 后，系统读取 `OIDC_DEPARTMENT_CLAIM` 指定的名称，并在全部现有组织节点中精确匹配：

- 只命中一个节点时，关联该节点；
- 没有命中或存在多个同名节点时，回落到集团根，并记录 warning；
- 已有用户再次登录时，也会按本次 claim 更新归属节点；
- 登录流程不会创建组织节点，不做模糊匹配，也不解析路径字符串。

## 排查登录失败

1. 检查 API 是否读取了 `OIDC_ENABLED=true` 和完整客户端配置。
2. 检查身份提供商登记的 Redirect URI 是否与 `OIDC_REDIRECT_URI` 完全一致。
3. 检查 API 能否访问 Issuer、JWKS 和 UserInfo 端点。
4. 检查 claims 中是否有 `sub` 和配置的用户名字段。
5. 查看 API 日志中的 OIDC 错误，但不要附带 Client Secret 或 Token。

登录页没有 OIDC 按钮，通常表示 OIDC 未启用或基础配置不完整；回调失败时，页面会返回登录页并显示可读错误。
