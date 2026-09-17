# 普通用户可自助修改自己的登录密码

状态：implemented
类型：feature
Owner：backend/server/routers/auth_router.py

## 问题

平台此前只有管理员能改密码：`PUT /api/auth/users/{id}` 需要 `user:update` 权限，`PUT /api/auth/profile` 的 `UserProfileUpdate` 只接受 `username` 与 `phone_number`。普通用户登录后在「账户设置」页看不到任何改密入口，前端 `AccountSettingsComponent` 也只提供头像、用户名与手机号。结果是用户的密码完全由他人设定：管理员设置一次、或通过运维手段批量初始化之后，用户无法自行更换，只能继续沿用别人知道的口令。

## 决策

新增一条只作用于当前登录用户的改密通道，且不引入新的权限点。

- `backend/server/routers/auth_router.py` 新增 `UserPasswordChange`（`old_password` 非空、`new_password` 用 `min_length=8`、`extra=forbid`）与 `PUT /api/auth/password`。该路由依赖 `get_required_user` 而不挂 `user:update`：修改自己的密码不属于用户管理权限，普通用户必须可用。
- 服务端以 `AuthUtils.verify_password` 校验原密码，不匹配返回 400「原密码不正确」；新密码与原密码相同返回 400。原密码校验是安全底线，令牌泄露时攻击者不能直接改密接管账号。
- 改密成功后调用 `reset_failed_login()` 清零失败次数、最后失败时间与锁定时间。否则一次旧的锁定会继续挡住新密码登录，用户会以为改密失败。
- 写入 `operation_logs` 的「密码已更新」，与 `update_user` 的既有文案一致。该文案同时是批量运维判断「某账号是否被改过密码」的判据，改密用户此后会被判为「已设置过」，正是期望语义。
- 前端 `web/src/apis/auth_api.js` 暴露 `changePassword`；`web/src/components/AccountSettingsComponent.vue` 在账户资料卡片内以「密码｜修改密码」一行文字按钮作为入口，点击打开弹窗（原密码、新密码、确认新密码）。页面加载不渲染任何密码输入框；打开前与关闭时清空输入；成功后提示并关闭弹窗，失败时保留弹窗便于直接修正。
- 前端 8 位下限与后端 `min_length=8` 对齐。`web/src/apis/base.js` 出于防泄露会把后端 400 的 detail 统一替换为「请求参数错误」，因此前端按 `error.status === 400` 补一句「请确认原密码是否正确」，否则用户无法判断失败原因。
- 弹窗由 antd 挂到 `body` 下，其表单样式写在样式块根层级（`.password-form`），不能嵌在 `.account-settings` 内，否则 scoped 祖先选择器不命中。

## 替代方案

把改密并入 `PUT /api/auth/profile`：可以少一个路由，但会让「改资料」变成可携带凭据的复合操作，鉴权与审计语义变模糊，未采纳。允许不填原密码改密（只要已登录即可）：对 OA 单点登录用户更省事，但令牌泄露即可接管账号，未采纳。改密后作废其他已登录会话：需要在 `users` 增加 `password_changed_at`，并在令牌校验处比对签发时间，改动扩散到登录链路与一次性建表迁移；本次接受「已签发令牌继续有效」的代价，未采纳。把强度提高到「字母+数字」或「12 位含符号」：会让用户自助改密的口径严于管理员设密口径（`UserCreate`、`UserUpdate`、`InitializeAdmin` 均为 8 位下限），未采纳。把入口做成常驻表单：用户反馈在账户设置页中过于突兀，改为按钮加弹窗。

## 后果

普通用户可自助更换密码，管理员不必代办，管理员在用户管理页的改密入口保持不变。改密后旧密码立即失效，因为登录按当前 `password_hash` 校验；已签发的 30 天令牌继续有效，其他设备上的会话不会被登出，要强制下线只能轮换 `JWT_SECRET_KEY` 并让所有用户一起掉线。新增的「密码已更新」日志会改变 `operation_logs` 的内容分布，这是该文案作为改密判据的前提，批量运维脚本会据此把改密账号判为「已设置过」。前端多了一次网络往返（成功后再关闭弹窗），弹窗打开期间密码以明文存在于组件响应式状态中，关闭时清空。

## 验证

单元测试 `backend/test/unit/server/test_auth_password_validation.py` 覆盖 `UserPasswordChange` 的 8 位下限、原密码允许短于 8 位（历史密码不受当前规则约束）、原密码不可为空、额外字段被拒绝，13 项通过。

真实 HTTP：未登录调用返回 401；原密码错误返回 400 且提示「原密码不正确」；新旧密码相同返回 400；新密码不足 8 位返回 422；正确改密返回 200；改密后旧密码登录 401、新密码登录 200。集成用例 `backend/test/integration/api/test_auth_router.py::test_standard_user_can_change_own_password` 固定同一组断言，并额外校验失败计数清零与「密码已更新」日志；该用例在本机为 `Not run`——本地 compose 没有 sandbox-provisioner 服务，`test/integration/conftest.py` 的 session 级 autouse 清理 fixture 直接失败，等价断言由真实 HTTP 脚本执行。

前端 `web/test/unit/accountPasswordChange.test.js` 断言接口路径与字段名契约（`old_password`、`new_password`）、弹窗入口装配、8 位与两次一致校验、400 提示；`eslint` 无输出，`vite build` 成功。

真实浏览器：本地构建产物经同源代理连真实后端，注入令牌后打开 `/settings/account`，断言页面加载时密码输入框为 0、点按钮后弹窗内为 3、保存后出现成功提示且弹窗自动关闭、重新打开表单为空、原密码填错时提示「请确认原密码是否正确」且弹窗保持打开；浅色、深色与错误态各留截图。
