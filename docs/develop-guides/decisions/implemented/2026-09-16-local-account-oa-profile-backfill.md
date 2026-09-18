# 本地账号按工号反查 OA 补齐展示姓名与岗位

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/oa_sso_service.py

## 问题

本地创建的账号（用户名即工号，如 `100001`）没有展示姓名：`users.display_name` 目前只由 OA 登录链路写入，用户管理接口也不接收该字段。结果是 `agents/USER.md` 的用户资料区块对这类账号只能显示登录账号，岗位等 OA 资料同样无法进入用户资料，模型拿不到真实身份信息。

## 决策

为 `users` 增加 `oa_station_name` 与 `oa_job_level_name` 列；业务 Schema 版本为 5，`storage-migrator` 接受版本 1 至 4 并幂等补列，DDL 成功后才记录新版本，API 与 worker 只校验版本，并让两条登录链路都写入岗位与职级：OA 免登录（`/api/auth/oa/exchange-token`、`/api/auth/oa/exchange-account`）在 `_complete_oa_login` 中同步 `identity.station_name` 与 `identity.job_level_text`（职级展示文本为「职级（职等）」），OA 未返回时保留既有值；密码登录 `POST /api/auth/token` 成功后调用 `backfill_local_user_oa_profile`，仅在 OA 用户接口已配置、用户不是 OA 身份（uid 不以 `oa:` 开头）、登录账号匹配 6~12 位纯数字工号时以账号反查 OA 用户信息接口，返回账号与配置的公司编码一致且为在职状态才采纳，写入 `display_name`、`oa_station_name` 与 `oa_job_level_name`。反查超时 3 秒，任何失败只记日志并返回 `False`，登录照常完成；改动随登录事务一并提交。`agents/USER.md` 资料区块在岗位、职级有值时才各增加一行，无值不产生空行；部门一行改为组织链路。

## 替代方案

在用户管理接口开放展示姓名字段，需要与「展示姓名由 OA 提供」的既有约束冲突，且同一份资料会出现两个来源。每次构建上下文时反查 OA 会让聊天链路依赖外部服务并放大延迟。把岗位连同其它 OA 字段一起落 JSONB 列，会在本期没有额外消费方的情况下提前定义存储结构。

## 后果

本地账号登录会多一次上限 3 秒的 OA 请求；OA 不可用、跨公司、离职或工号格式不符时本地资料保持原样。任何用户名形如工号的本地账号都能领取该工号在 OA 的姓名与岗位，因此该能力适用于受信内网与统一工号体系；工号或姓名在 OA 侧变更后，下次登录会自动刷新。展示姓名与岗位不参与登录、鉴权与权限判断，OA 身份仍由 `oa:<companyCode>:<account>` 唯一确定。

## 验证

单元测试覆盖：职级展示文本组合（职级（职等）／缺一项／全缺）、OA 免登录时岗位与职级落库、OA 未返回时保留既有岗位与职级、本地账号反查补全成功并写入三列、重复反查不产生改动、OA 身份与非工号账号不发起请求、账号/公司编码/在职状态校验不通过时不改写、OA 异常时只返回 `False` 不抛错；`get_memory_profile` 的列序断言、组织链路退回部门名与 USER.md 的岗位与职级行（有值写入、无值不写）。集成测试通过独立认证进程访问受控 OA HTTP 服务，覆盖本地账号补全成功、上游失败后登录继续和三列保持原样，并回读 PostgreSQL 确认事务提交。迁移测试从缺少两列的 v4 隔离 Schema 执行真实 `storage_migration.main`，验证补列、记录 v5、保留展示姓名及重复迁移保留岗位职级。画像测试通过上下文入口回读文件和模型输入。真实外部 OA 服务及生产部署不在这些确定性检查的覆盖范围内。
