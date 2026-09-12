# 保持登录期限调整为 30 天并按勾选选择令牌存储范围

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/utils/auth_utils.py

## 问题

登录页"保持登录"勾选项原先只影响前端存储，而后端 JWT 默认有效期固定为 7 天，两者语义不一致：用户勾选"保持登录"后第 8 天仍会被强制重新登录，与界面承诺不符。服务端只有一个默认有效期，无法按是否勾选区分令牌寿命。

## 决策

1. 后端 `JWT_EXPIRATION` 默认有效期由 7 天调整为 30 天，与"保持登录"的界面承诺对齐。
2. 前端按勾选状态选择存储范围：勾选写入 `localStorage`（跨浏览器会话保留），未勾选只写入 `sessionStorage`（关闭标签页即失效）。
3. OIDC 登录复用同一选择：登录页把选择写入 `oidc_remember_login`，回调页读取后决定令牌存储范围。
4. 登录页移除《用户协议》《隐私协议》勾选同意入口与 `ensureAgreementAccepted` 提交门禁，该变更已获合规确认可下线。

## 替代方案

- 只改前端存储、不改后端有效期：勾选"保持登录"的用户仍会在第 8 天被登出，未解决语义不一致。
- 为未勾选用户签发短时效令牌：需要给 `/api/auth/token` 增加参数并改动签发与校验链路，改动面与本次期限调整不匹配；当前依赖 `sessionStorage` 限制客户端留存。
- 保留协议同意门禁：与期限调整无关，且已获合规确认可下线。

## 后果

- 未勾选"保持登录"的用户同样获得 30 天有效令牌，仅靠 `sessionStorage` 限制客户端留存；令牌一旦泄露，可用窗口由 7 天扩大到 30 天。该取舍已确认接受并在 PR 正文申报。
- 登录页不再承担协议告知职责，改由公司统一流程承接。
- 服务端仍只有单一有效期配置；未来若要求按勾选状态区分令牌寿命，需重新设计签发链路。

## 验证

- `docker exec fix-test-workflow-api-1 python -m pytest test/unit/test_auth_utils.py -q` → 20 passed。
- `eslint`（4 个改动文件）通过；`vite build` 通过；`node --test test/unit/authSessionAbort.test.js test/unit/api_boundary.test.js` → 12 passed。
- `git diff --check` 通过。
- 未验证：真实浏览器交互与"第 31 天到期"行为未做端到端验证。
