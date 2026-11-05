# 数据总览对历史 JSON 空字符的统计兼容

状态：implemented
类型：bug-fix
Owner：backend/yuxi/modules/system/repositories/dashboard.py

## 问题

工具结果可在 PostgreSQL json 中保存 NUL 转义，但统计提取工具名时 PostgreSQL 会对文档内容解码并拒绝 NUL。单条二进制输出审计因此阻断工具概览和时间序列，前端聚合读取失败后只能显示基础指标。上游 main 220ae2b65fee8419fbb555a76387f995a99acb20 尚无此兼容处理。

## 决策

### 实现方案

数据总览页面、指标卡片和知识库图例对齐上述最新上游相关实现，保留 fork 的组织范围授权。

Dashboard repository 在工具名、模型名统计的 SQL 投影中移除真实 NUL 转义，再执行原有 JSON 字段读取和数据库聚合。按反斜杠奇偶区分真实转义与字面量文本，不把字面量反斜杠 u0000 当作空字符。SQLite 测试沿用原生 JSON 表达式。该表达式只供统计，不改写存储的审计内容。

## 替代方案

不删除或清洗用户历史记录，不把失败的统计伪装为零，也不将完整历史载入 Python 内存再统计。不扩大公开接口或放宽组织权限。

## 后果

统计口径与聚合位置保持不变，查询增加 JSON 文本规范化成本。修复只针对已复现的 NUL 转义，不声称支持所有损坏 JSON 或所有非法 Unicode 输入。

## 验证

backend/test/integration/api/test_dashboard_organization_scope.py 新增真实 PostgreSQL/HTTP 用例，覆盖嵌套 NUL、连续 NUL、反斜杠加 NUL、字面量反斜杠 u0000、引号及中文工具名、模型时间序列和跨组织隔离，并回读原审计 JSON 不变。该用例在修复前复现 UntranslatableCharacterError，修复后与 Dashboard 单测共 22 项通过。

参考 PostgreSQL 16 的 [JSON 类型说明](https://www.postgresql.org/docs/16/datatype-json.html)：json 允许存储的 Unicode 转义，在查询处理阶段仍可能被拒绝。
