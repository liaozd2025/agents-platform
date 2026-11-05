# 基于最新上游的侧栏会话来源过滤

状态：implemented
类型：bug-fix
Owner：backend/yuxi/modules/agents/repositories/sessions.py

## 问题

智能体创建的 root 会话没有父节点，被上游侧栏当作普通历史显示。仅过滤子会话不能满足用户只在侧栏查看主动创建会话的要求。

## 决策

### 实现方案

以 xerrors/Yuxi main 220ae2b65fee8419fbb555a76387f995a99acb20 为基线，移入相关侧栏归档、当前归档详情只读及终态提示修复。保留上游 create_session 的 child 默认值与 root 能力。

按用户确认增加 Web 历史读取入口，在数据库分页前限定 parent_thread_id 和 created_by_run_id 均为空。Public 会话 API 保持原有完整会话语义。前端由历史接口确认列表成员，独立缓存自动任务详情，侧栏和搜索最近记录继续执行上游未归档过滤。直接 URL 导航显式允许先缓存归档详情，再设置当前 ID；其详情只读且不进入历史，避免上游缓存准入与路由初始化顺序冲突。

## 替代方案

不禁止上游 root 模式、不重写历史会话树，不新增数据库字段或删除自动会话。不仅在前端隐藏一页数据，避免自动任务占用分页名额。

## 后果

自动子会话和历史自动 root 不进入侧栏；用户新建、分页与置顶保留。原会话协作结果、来源链接、主动搜索和直达 URL 仍可访问已授权任务记录。

## 验证

真实 PostgreSQL 与 HTTP 验证历史分页、置顶及用户/APP 隔离，确认原任务导航与完整列表不变；真实前端模板验证自动 root、子会话与归档项隐藏；Store 验证详情缓存、用户创建及翻页。上游协作工具模式保持原契约。backend/test/integration/api/test_session_history.py 通过真实 HTTP 与 PostgreSQL 回读验证上述行为；frontend/test/unit/sessionSidebarVisibility.test.js 渲染真实侧栏模板验证隐藏结果。浏览器控制通道不可用，未完成真实浏览器截图验收。
