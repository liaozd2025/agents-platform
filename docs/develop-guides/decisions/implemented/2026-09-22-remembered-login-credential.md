# 登录页记住账号密码并在退出后自动回填

状态：implemented
类型：feature
Owner：web/src/utils/rememberedLogin.js

## 问题

登录页的「保持登录 30 天」勾选此前只决定访问令牌写 `localStorage` 还是 `sessionStorage`（`web/src/stores/user.js` 的 `persistToken`），与账号密码无关；登录页也没有任何回填逻辑，`loginForm` 每次挂载都是空值。用户勾选后退出登录，重新进入登录页仍需完整输入账号密码，与「保持登录」的直觉预期不一致。

浏览器自带的密码管理器不构成可用替代：登录页的账号与密码输入框由 antd 4 的 `a-input` / `a-input-password` 渲染，实测 DOM 上 `name` 与 `autocomplete` 均为空串，`id` 为 `form_item_loginId` / `form_item_password`。缺少登录表单语义时浏览器不易弹出「保存密码」，也不保证回填，且行为受浏览器设置、无痕模式与企业策略影响，无法形成可验收的产品行为。

## 决策

在应用内实现「记住密码」，并把凭据的持有权交给一个独立模块 `web/src/utils/rememberedLogin.js`：

- 存储 key 为 `remembered_login`，值为 base64 编码的 JSON `{ loginId, password }`。编码用 `TextEncoder` 取 UTF-8 字节后再 `btoa`，避免中文账号或含非 ASCII 字符的密码触发 `InvalidCharacterError`；base64 只用于避免在 DevTools 中直读，**不是加密**，不作为安全边界。
- 复用既有的「保持登录 30 天」勾选作为唯一开关，不新增第二个复选框：勾选 = 令牌跨会话保留 + 凭据本地保留；取消勾选 = 两者都清理。同一语义只暴露一个交互面，避免「勾了一个却记不住密码」的困惑。
- 写入时机固定在登录成功之后（`handleLogin` 中 `userStore.login` 返回之后）：失败登录不落盘，避免把错误密码记住并回填。勾选时写入，未勾选时清除上次残留，保证勾选状态与本地数据始终一致。写入失败（浏览器禁用站点数据、存储配额耗尽）时 `saveRememberedLogin` 返回 `false`，登录页以 warning 提示「未能记住本次登录密码」，不把「未记住」表现为静默成功。
- 读取时机在登录页 `onMounted` 中、`infoStore.loadInfoConfig()` 之前：回填 `loginForm.loginId` 与 `loginForm.password`，并把勾选框同步为选中，保证显示状态与实际行为一致。放在信息接口请求之前是为了缩短表单从空到有值的可见时间。
- 取消勾选时立即清除（`handleRememberLoginChange`），不必等到下次登录成功，避免用户取消勾选后仍被自动填入密码。
- 退出登录**不清除**凭据：`userStore.logout()` 只清访问令牌，与本次新增的本地凭据互不影响，这是「退出后仍自动回填」这一需求的直接前提。
- 所有读写对存储异常降级：隐私模式禁用 `localStorage`、内容被手工改坏、历史格式残留（只有账号没有密码、字段类型不对）时，读取返回 `null` 并顺手清掉脏数据，不抛异常、不影响登录页初始化。**访问 `localStorage` 属性本身也会抛错**（浏览器禁用站点数据时抛 `SecurityError`），而回填发生在登录页初始化的最前面，若不兜住会连带中断其后的品牌信息加载、健康检查、首次运行检查与 OIDC 自动登录，故 `getStorage()` 与所有 `getItem` / `setItem` / `removeItem` 调用都在 try 内，失败只记录原因（不打印凭据）。

## 替代方案

- 完全依赖浏览器密码管理器：给输入框补 `name` 与 `autocomplete="username"` / `"current-password"`，由浏览器负责保存与回填。应用侧不持有密码，安全性更好，但回填与否取决于浏览器实现、用户是否点击「保存」、无痕模式与企业策略，无法保证行为，也无法作为验收依据。本次未采用，但保留为后续可选增强；若采用需与当前回填逻辑共存（两者填入同一组账号密码，不会冲突）。
- 只记住账号、不记密码：安全性最高，但直接不满足「退出后密码自动填上」的诉求。
- 加密存储密码：密钥必须与密文同处前端可读位置，等价于混淆，只能增加实现复杂度与故障面，不提升实际防护能力，故用 base64 并在注释中写明其非加密性质。
- 新增后端接口下发「记住密码」令牌：把密码保管责任转移到服务端，需要新的持久化与权限面，超出该交互优化的范围。
- 独立新增一个「记住密码」复选框：多一个与「保持登录」语义重叠的交互面，用户需要理解两个勾选的区别，认知负担大于收益。

## 后果

- 勾选状态下，退出登录或关闭浏览器后重新进入登录页，账号与密码会被自动填入并勾选「保持登录 30 天」，用户只需点击登录。
- 密码以 base64 形式落在浏览器 `localStorage`，同源脚本（含 XSS）可解出明文。本机共用设备的场景下不应勾选；该约束已写入模块头部注释。
- `remembered_login` 与访问令牌存放在同一存储域但彼此独立：令牌过期或服务端踢出登录不会连带清除凭据，凭据也不会延长令牌有效期。后端令牌仍是 30 天固定有效期，本次未改变认证有效期语义。
- 凭据仅在登录页使用，不参与鉴权、不进入请求体以外的任何链路；`saveRememberedLogin` 的入参账号会 `trim`，密码保持原样（密码中的空格可能有效）。
- 脏数据自愈：`loadRememberedLogin` 遇到无法解析或字段不全的内容会删除该键，避免每次进入登录页反复解析同一份坏数据。
- 未覆盖：多账号场景下只保留最后一次成功登录的凭据，切换账号会覆盖；无痕模式下勾选无实际效果（存储随会话销毁）。
- 已知边界（基线遗留，本次不扩大范围）：`web/src/stores/user.js` 的 `restorePersistedToken()` 在模块加载时直接调用 `localStorage.getItem`，浏览器禁用站点数据时同样会抛 `SecurityError`。本次只保证新增模块不引入该问题，没有修改这个既有调用点。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 勾选后登录成功即把账号与密码写入本地 | 勾选后仍不回填 | `web/src/utils/rememberedLogin.js` | 真实页面（Playwright，mock `/api/auth/token` + `/api/auth/me`）场景 A：勾选 → 提交 → `remembered_login` 落盘，解码后与输入一致 | 场景 C：mock 401 登录失败 → 该键仍为 `null`；单测「账号或密码为空时不写入，也不覆盖已有凭据」 | Passed |
| 进入登录页自动回填账号与密码并同步勾选框 | 退出后密码仍为空 | `web/src/utils/rememberedLogin.js`、`web/src/views/LoginView.vue` | 真实页面：预置凭据 → 重新进入 → `#form_item_loginId` 为 `demo_user`、`#form_item_password` 为 `P@ssw0rd中文`、勾选框选中（`mem_login_e2e.cjs` 5/5） | 场景 D：取消勾选后重新进入 → 密码框为空、勾选框未选中 | Passed |
| 取消勾选立即清除已保存凭据 | 取消勾选后仍自动填充 | `web/src/views/LoginView.vue` | 真实页面：点击取消勾选 → `localStorage.getItem('remembered_login')` 为 `null` | — | Passed |
| 编码支持中文与非 ASCII 字符且落盘非明文 | 中文账号/密码导致写入抛异常或无法回读 | `web/src/utils/rememberedLogin.js` | `node --test test/unit/rememberedLogin.test.js` 11 passed；落盘值等于独立实现（Python base64）预先算出的常量 | 解码失败、非 JSON、字段缺失或类型不对三种脏数据均返回 `null` 并清除该键 | Passed |
| 本模块的读写在存储不可用或被禁用时不抛异常 | 禁用站点数据时回填环节抛错，中断登录页初始化 | `web/src/utils/rememberedLogin.js` | 单测覆盖三种环境：无 `localStorage`、`getItem`/`setItem`/`removeItem` 抛错、访问 `localStorage` 属性本身抛 `SecurityError`，本模块读写均不抛错 | — | Passed |
| 落盘失败对用户可见 | 写入失败却被当成已记住 | `web/src/views/LoginView.vue` | `handleLogin` 中按 `saveRememberedLogin` 返回值提示 warning；单测覆盖写入被拒返回 `false` | — | Inspected（提示文案未做真实浏览器验证） |
| 改动不破坏前端构建与既有约束 | 编译或静态检查失败 | `web/src/views/LoginView.vue` | 容器内 `node --test`（11/11）、`npx eslint`（通过）、`pnpm run build`（成功） | — | Passed |

执行命令与结果：

- `docker exec test-web-1 sh -c "cd /app && node --test test/unit/rememberedLogin.test.js"`：11 passed。
- `docker exec test-web-1 sh -c "cd /app && npx eslint <4 个改动文件>"`：通过。
- `docker exec test-web-1 sh -c "cd /app && pnpm run build"`：构建成功。
- 真实页面验证：`.workbuddy/tmp/mem_login_e2e.cjs` 5/5 通过（回填、勾选同步、取消勾选清除、无页面异常）；`.workbuddy/tmp/mem_login_save_e2e.cjs` 9/9 通过（登录成功落盘、未勾选清除残留、登录失败不落盘、取消勾选后重新进入不回填）。
- 未执行项：真实后端账号的完整登录回归（页面验证用 mock 替代 `/api/auth/token` 与 `/api/auth/me`）；多标签页并发操作凭据；落盘失败的 warning 提示未在真实浏览器中触发验证。
- 说明：容器内 `npx prettier --check src/views/LoginView.vue` 会报格式不合规，但**远端 main 上的同名文件同样不合规**（已用基线文件对照验证），属仓库基线问题；仓库的前端格式门禁为 `eslint`（配置了 `@vue/eslint-config-prettier/skip-formatting`），本次改动未引入新的格式违规。

## 独立语义 Review

由不继承开发上下文的独立 Reviewer Agent 完成两轮（2026-09-22），覆盖需求、完整 diff、测试与决策记录：

- 首轮结论：阻断 1 项（`getStorage()` 与 `getItem` 未兜住浏览器禁用站点数据时的 `SecurityError`，会中断 `onMounted` 后续流程）、重要 3 项（验证表两处 `Passed` 缺对应断言；回填处注释与代码位置不符；`saveRememberedLogin` 返回值被丢弃导致写入失败被静默）。另有次要项：单测中一处用 `btoa` 与被测同源编码、一条近乎恒真的 `notEqual` 断言、`console.warn` 直接打印 error 对象。
- 处理：上述问题全部修复（详见「决策」与「验证」章节）；`console.warn` 改为只打印 `error?.message`，避免凭据或完整对象进入日志。
- 复审结论：4 项均确认为已修复；对阻断项做了变异验证（在临时副本上还原 `getStorage()` 的 try，第 11 条用例随即变红），确认该用例能复现原缺陷。复审另指出 3 个次要问题（验证主张表述偏宽、决策记录一处措辞过期、脏数据用例缺清除断言），已在本记录与单测中修正。
- 未从静态审查验证：两个浏览器脚本的 9/9 与 5/5 结果、以及落盘失败 warning 的真实弹出效果。

## 关系

扩展 [保持登录期限调整为 30 天并按勾选选择令牌存储范围](2026-09-12-remember-login-30-days.md) 与 [登录页视觉改版](2026-09-20-login-page-visual-refresh.md) 确立的「保持登录」语义：令牌持久化规则不变，同一勾选新增「凭据本地保留」职责。
