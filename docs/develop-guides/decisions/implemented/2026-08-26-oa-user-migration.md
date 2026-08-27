# OA 用户迁移

状态：implemented
类型：feature
Owner：backend/scripts/migrate_oa_users.py

## 问题

需要将旧 OA 在职人员迁移到当前平台。旧 OA 的 `account` 对应当前 `users.username`，但两套系统的部门主键、密码、角色和会话不能直接复用。

## 决策

使用独立脚本调用旧 OA `DrugDevp/QueryUserPage` 接口，默认 dry-run。部门树同步时回填旧 OA 的 `treeCode` 与节点 `id`；人员接口中的 `departmentId` 可能为 `200004.0`，迁移脚本将其规范化为整数后精确匹配节点 `id`，不按名称关联。新用户使用与 iframe SSO 一致的稳定 `uid` `oa:<companyCode>:<account>`、随机密码哈希和内置 `user` 角色；已有用户补齐该稳定 UID，并按需更新 `department_id`。稳定 UID 已被其他用户占用时报告冲突并拒绝写入，避免同一 OA 身份被错误合并。空字段、格式无效、重复账号、部门缺失或重名均跳过并记录。只有显式 `--apply` 才在单事务中写库。

补充约束：旧 OA 的 `nickName`（对应 `sys_user.nick_name`）优先写入可空的 `users.display_name`；`fullName` 和 `userName` 仅作旧接口兼容回退。该字段只用于管理界面展示；`users.username` 始终保留旧 OA `account`，继续作为登录账号和迁移匹配键。

## 替代方案

- 直接把旧 OA 部门编码写入 `department_id`：拒绝，两个系统的主键没有同源约束。
- 自动按名称创建部门：拒绝，无法从人员接口还原完整组织树，可能改变权限语义。
- 覆盖已有用户资料和密码：拒绝，当前平台用户资料与安全信息由当前系统负责。

## 后果

迁移前必须先在当前平台补齐可唯一匹配的部门；未匹配人员需要人工处理。该脚本与 H5 历史会话迁移保持独立，后续可分别提 PR。

## 验证

部门同步沿用同一只读授权文件和 dry-run 约束，先创建部门树再执行用户迁移；不会复用 OA 部门主键，也不会覆盖当前平台已有部门。

- `python -m py_compile backend/scripts/migrate_oa_users.py backend/test/unit/scripts/test_migrate_oa_users.py` 已通过。
- `git diff --check` 已通过。
- 容器单元测试和 Ruff 未能执行：当前 Compose 根目录缺少 `.env`，已有 `api-dev` 容器未挂载本工作区，宿主机未安装 pytest。
