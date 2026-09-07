# H5 历史会话迁移绑定用户项目

状态：implemented
类型：bug-fix
Owner：backend/scripts/migrate_jd_ai_h5_conversations.py

## 问题

当前 `conversations.project_id` 为非空字段，旧版 H5 历史会话迁移脚本没有写入项目标识，导致 PostgreSQL 拒绝插入历史会话。

## 决策

迁移写入阶段按 OA 用户 UID 创建或复用一个幂等的 managed Project，并将该项目绑定到该用户的每条历史会话。项目使用固定幂等键，重复执行迁移时复用已有项目。

## 替代方案

- 允许 `conversations.project_id` 为空：会破坏当前 Project/Workdir 约束和权限查询，不采用。
- 所有 OA 用户共用一个项目：Project 归属单一 UID，会绕过用户隔离，不采用。

## 后果

每个有历史会话的 OA 用户会多一个“OA历史会话”项目；预检模式不创建项目，只有 `--apply` 才会写入项目和会话。

## 验证

- 单元测试验证项目 UID、managed Workdir 和幂等键。
- 服务器先执行 5 条 `--apply` 试迁移，确认会话可见后再执行全量迁移。
