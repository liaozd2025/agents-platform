# 沙箱任务采用 v0.7.3 执行方案

状态：accepted

删除 fork 的 PI Agent 沙箱统一委派功能，子智能体方案发生冲突时以上游 v0.7.3 为准；保留上游普通子智能体与沙箱能力。历史 PI 结果继续可查看、可下载，旧 PI 任务不再续跑，新请求使用 v0.7.3 方案。该方向取代 [混合 PI 执行](./0004-hybrid-pi-execution-seam.md)中的新增 PI 执行选择；完整删除边界与验收由[实施决策](../develop-guides/decisions/implemented/2026-09-18-retire-pi-executor.md)维护。
