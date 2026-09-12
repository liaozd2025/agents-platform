# 并发 Skill 投影复用

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/agents/skills/service.py

## 问题

多个 worker 可能同时向同一个 Skill 线程目录投影相同的只读 Skill。Windows bind mount 在一个 worker 已创建目标目录后，可能将另一个 worker 的临时目录重命名报告为权限拒绝，进而使子智能体启动失败。

## 决策

目录重命名失败后，仅当目标是非符号链接目录且其内容与当前 Skill 来源完全一致时，复用该目标目录并继续执行。其他异常和内容不一致仍保留原有失败行为。

## 替代方案

使用跨进程锁可避免竞争，但需要新增持久化协调机制。直接忽略所有重命名失败会掩盖真实的存储或权限故障。

## 后果

同内容的并发投影可在 Windows 本地 Docker 环境正常完成；不同来源的覆盖竞争仍会显式失败，避免错误复用。

## 验证

`backend/test/unit/services/test_skill_service.py` 覆盖另一 worker 已创建等价目标目录且本次重命名收到 `PermissionError` 的负向场景，断言同步成功并清理临时目录。
