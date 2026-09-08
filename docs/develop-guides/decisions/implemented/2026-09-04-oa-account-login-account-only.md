# OA 账号换票仅接收账号

状态：implemented
类型：simplification
Owner：`backend/package/yuxi/services/oa_sso_service.py`、`web/src/utils/oaEmbedBridge.js`

## 问题

现有 OA 父项目只能向 iframe 提供账号，无法增加共享密钥、时间戳和签名逻辑。账号换票必须能在生产环境使用该既有接入方式。

## 决策

`/api/auth/oa/exchange-account` 只接收账号，不读取共享密钥，也不校验签名请求头。服务端使用账号调用已配置的 OA 换票接口，随后携带换得的 `oaToken` 调用 OA 用户信息接口，继续校验返回账号、公司编码和在职状态后才创建或复用 Yuxi 用户。

## 替代方案

保留 HMAC 签名能降低账号被伪造后的冒用风险，但需要修改 OA 父项目，不满足当前接入约束。继续使用 OA token 交换也可行，但父项目当前无法提供 token。

## 后果

部署只需配置 `OA_ACCOUNT_LOGIN_ENABLED`、`OA_ACCOUNT_LOGIN_URL` 和 `OA_ACCOUNT_LOGIN_COMPANY_CODE`，不再配置 `OA_ACCOUNT_LOGIN_SHARED_SECRET`。该接口的访问必须受可信 OA 网络、网关或来源隔离保护；若未来 OA 父项目支持服务端签名，可重新评估并引入签名校验。

## 验证

后端单元测试验证生产环境在只配置账号换票参数时可用，并验证账号换票仍通过 OA 返回凭证和用户信息校验。前端单元测试验证 iframe 只向允许来源请求并传递账号，不再传递签名或时间戳。
