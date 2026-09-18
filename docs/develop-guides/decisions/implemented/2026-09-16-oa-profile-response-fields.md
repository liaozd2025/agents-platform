# OA 登录响应透出展示资料字段

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/oa_sso_service.py

## 问题

OA 用户信息接口（`OA_SSO_USERINFO_URL`）返回的展示资料远多于 Yuxi 当前使用的字段：主岗岗位、职级、职等、业务分部、上级姓名、公司全称、入职日期、性别、手机号与头像地址。当前只把展示姓名与主部门落进本地用户，前端在登录后拿不到岗位等信息，用户资料与会话列表缺少可识别的身份信息。

## 决策

`fetch_oa_identity` 在既有身份校验（账号一致、公司编码、在职状态）通过后，把主岗的 `appointmentStationName`、`jobLevelName`、`jobGradeName`、`businessDivisionName`、`superiorPostLeaderAccountName`、`companyFullName`，以及 `inductionDate`、`sexName`、`mobileNumber`、头像 `photoGraphFileName` 投影进 `OAIdentity` 的新增可选字段；`_complete_oa_login` 在两个 OA 登录响应里新增 `oa_profile` 对象，认证路由的 `Token` 响应模型声明该可选字段，保留 HTTP 序列化后的展示资料。

头像字段在网关是 JSON 字符串，解析后取第一条记录的 `url` 与 `downUrl`，保持 OA 给出的相对路径原样透出；解析失败或结构不符时记中文日志并把头像置空，登录继续。展示姓名、岗位与组合职级进入本地用户行（`display_name`、`oa_station_name`、`oa_job_level_name`，供 `agents/USER.md` 使用）；OA 响应中的 `job_level_name` 保留原始职级，画像使用组合职级。新建 OA 用户的 `users.phone_number` 与 `users.avatar` 为 `null`，既有用户的手工值保持不变，`department_id` 的既有写入逻辑不变。

## 替代方案

把全部展示字段写入 `oa_profile` JSONB 列会增加没有当前消费方的持久数据；保留姓名、岗位、职级三个用户画像字段，其余字段只随 OA 登录响应返回。把手机号写入 `users.phone_number` 还叠加唯一索引冲突与个人信息存储面扩大的代价，未采纳。

## 后果

`/api/auth/oa/exchange-token` 与 `/api/auth/oa/exchange-account` 的响应多出 `oa_profile`，OA 未返回的键为 `null`；`/api/auth/me`、用户列表等接口保持原样，前端在不读取该字段时行为与改动前一致。响应体含手机号，需按内网接口对待，不写入日志、不外发。手机号与头像要进入本地资料时，仍需单独决策并处理唯一索引与用户自维护数据的优先级；展示姓名、岗位与职级在本地账号登录时的落库链路见 [2026-09-16-local-account-oa-profile-backfill.md](2026-09-16-local-account-oa-profile-backfill.md)。

## 验证

单元测试覆盖：主岗按 `pagingSort` 最小选取、全部展示字段映射、头像 JSON 字符串解析、头像字段非法或缺失时登录照常完成、登录响应包含 `oa_profile`，以及 `users.phone_number` 与 `users.avatar` 在登录后仍为空。真实 HTTP 集成测试分别调用两条 OA 登录接口，检查响应 `oa_profile` 保留岗位和原始职级，并从 PostgreSQL 回读姓名、岗位和组合职级。移除响应模型中的 `oa_profile` 会使两条路由测试因缺少该键失败；外部 OA 服务不属于本地受控测试的覆盖范围。
