# 第三方登录认证
Yuxi 支持以OIDC接入第三方登录认证，方便企业用户集成现有的身份认证系统。
> 此功能默认关闭，需要在配置文件中启用并提供相关参数。

## 配置步骤
### 1. 前提条件
在你的SSO系统中注册一个新的客户端应用，获取以下信息：
- 客户端ID（Client ID）
- 客户端密钥（Client Secret）
- ISSUER URL
- Provider discovery 地址或 JWKS URL

填入回调地址（Redirect URI）：https://<your_yuxi_host>/api/auth/oidc/callback

### 2. 配置Yuxi
在Yuxi的.env文件中添加以下配置项：

```sh
# 是否启用 OIDC 认证 (true/false)
# OIDC_ENABLED=false

# 认证源名称（显示在登录按钮上的文字，建议简短且具有辨识度, 默认: OIDC登录）
# OIDC_PROVIDER_NAME="OIDC登录"

# OIDC Provider 的 Issuer URL (例如: https://auth.example.com)
# OIDC_ISSUER_URL=

# OIDC Client ID
# OIDC_CLIENT_ID=

# OIDC Client Secret
# OIDC_CLIENT_SECRET=

# OIDC 回调 URL (可选，默认自动构建为 /api/auth/oidc/callback, 不建议自定义)
# 填写完整的地址：https://<your_yuxi_host>/api/auth/oidc/callback
# 需要确保此 URL 在 OIDC Provider 中已注册
# OIDC_REDIRECT_URI=

# 授权端点 (可选，自动从 discovery 获取)
# OIDC_AUTHORIZATION_ENDPOINT=

# Token 端点 (可选，自动从 discovery 获取)
# OIDC_TOKEN_ENDPOINT=

# UserInfo 端点 (可选，自动从 discovery 获取)
# OIDC_USERINFO_ENDPOINT=

# JWKS 地址（使用显式端点时必填，discovery 模式自动获取）
# OIDC_JWKS_URI=

# 登出端点 (可选，自动从 discovery 获取)
# OIDC_END_SESSION_ENDPOINT=

# 请求的 scope (默认: openid profile email)
# OIDC_SCOPES=openid profile email

# 是否自动创建用户 (true/false，默认: true)
# OIDC_AUTO_CREATE_USER=true

# OIDC 首次登录创建的新用户固定获得内置 user 角色，管理员可在用户管理中调整

# 用户名映射字段 (默认: preferred_username)
# OIDC_USERNAME_CLAIM=preferred_username

# 邮箱映射字段 (默认: email)
# OIDC_EMAIL_CLAIM=email

# 姓名映射字段 (默认: name)
# OIDC_NAME_CLAIM=name

# 是否使用原始用户名（不带 oidc: 前缀），允许映射到 Yuxi 已有的本地账号 (true/false，默认: false)
# 开启后，OIDC 返回的 username 会直接作为业务登录标识 uid 登录，需要管理员提前创建好用户账号
# OIDC_USE_RAW_USERNAME=false

# 是否从 OIDC userinfo 中获取部门 claim (true/false，默认: false)
# OIDC_FETCH_DEPARTMENT_INFO=false

# 部门名称字段映射 (默认: department)
# OIDC_DEPARTMENT_CLAIM=department

# OIDC 登录时是否强制提示用户重新登录 (添加 prompt=login 参数，true/false，默认: true)
# OIDC_FORCE_PROMPT_LOGIN=true

```
### 3. 重启Yuxi服务使配置生效
```bash
docker restart api-dev web-dev
```

## 功能说明

### 使用原始用户名（OIDC_USE_RAW_USERNAME=true）
当你需要将 Yuxi 系统中已有的本地账号与 OIDC SSO 绑定，可以开启此选项。

**绑定原理**（无需修改数据库）：  
系统会创建一个标记为删除的占位用户 `oidc:{sub}:{target_user_id}` 来记录 OIDC sub 与 Yuxi 用户的绑定关系，确保只有绑定过的 OIDC 身份才能登录对应的账号，**防止账号冒用**。其中 `target_user_id` 是数据库中的数值 `users.id`；用户登录标识仍使用字符串 `uid`。

### 身份令牌与部门信息

系统会验证 `id_token` 的签名、`iss`、`aud`、`exp`、`iat`、`sub` 和 `nonce`。使用显式端点配置时，必须同时设置 `OIDC_ISSUER_URL` 和 `OIDC_JWKS_URI`；discovery 返回的 `issuer` 必须与 `OIDC_ISSUER_URL` 完全一致。OIDC 端点必须使用 HTTPS，仅 `YUXI_ENV=development` 时允许本机 HTTP Provider。

开启 `OIDC_FETCH_DEPARTMENT_INFO` 后，系统会从 OIDC claims 的 `OIDC_DEPARTMENT_CLAIM` 字段读取组织节点名称，仅在全部已有组织节点中精确命中一个时关联用户；已有用户再次登录也会按本次 claim 更新归属节点。

- claim 命中 0 个或多个同名节点时，用户回落到集团根，并写入 warning 日志
- 登录流程不会创建组织节点，不做模糊匹配或路径字符串解析
