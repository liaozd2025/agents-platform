# 搜索来源列表的内外链跳转与嵌入导航协议

状态：implemented
类型：feature
Owner：web/src/utils/sourceNavigation.js

## 问题

知识库来源与网络搜索来源的结果列表都以新标签直接打开 `url`。OA 内嵌场景下这些链接指向 OA 站点，新标签会把用户带离当前会话页面；jd-ai-h5 已有做法是由父页面接管路由跳转，但 Yuxi 的嵌入桥没有导航类消息，且后端产出的 OA 地址把文章类型放在路径段、任务号放在 hash 内查询串，与 jd-ai-h5 的解析约定不一致，无法直接复用其解析函数。

## 决策

来源列表点击统一经由 `web/src/utils/sourceNavigation.js` 分发。`isOAInternalUrl` 按精确域名判定内部链接；`parseOANavigateParams` 依次从 hash 内查询串、常规查询串读取 `taskId`（`taskId`/`taskID`/`task_id`）与 `ecType`（`ecType`/`type`），并在缺参时从 `view-page/{n}` 路径段兜底。内部链接且参数完整时，嵌入桥以 `new-navigate` 消息把 `{taskId, ecType}` 交给父页面做路由跳转，当前 iframe 保留；其余情况一律新标签打开。

本文档与同一目录下的 `2026-09-12-knowledge-source-merge.md` 分工：后者拥有来源元数据的生成与分组，本文档拥有点击之后的跳转行为。

后端 `source_references.py` 在 OA 地址中同时写入 `ecType`，取值与 `page_type` 同源：1 表示好文共享，3 表示新闻详细。新数据因此不再依赖前端兜底；路径段兜底保留，用于已持久化在消息元数据里的历史地址。

前端来源组件保留 `href` 与 `target`，只接管不带修饰键的左键点击，修饰键、中键与无障碍访问仍走浏览器原生行为。

## 替代方案

父页面协议改发通用 `navigate`（`{url, target}`）可避免与 jd-ai-h5 对齐，但要求父插件自行解析 OA 地址格式；两种消息同时发送会在父页面造成重复跳转。只补后端参数而不做前端兜底会让已入库的历史地址永久点不动。两者均未被采用。

## 后果

新增一条对外 postMessage 契约 `new-navigate`。只要嵌入桥处于活动状态，来源点击就走父页面跳转，不再有新标签兜底：父插件未实现该消息时点击不产生任何跳转，这是当前接受的代价，协议实现与否需要与 OA 侧确认。OA 地址新增查询参数会改变新写入来源的字节内容，已入库的历史地址不受影响。`ecType` 的语义由 OA 父插件定义，取值域需要与 OA 侧核对。

## 验证

`web/test/unit/sourceNavigation.test.js` 覆盖域名判定、正式与历史两种地址格式解析、缺参返回 `null`、嵌入态父页跳转、外部链接新标签以及非嵌入与解析失败的降级；`web/test/unit/oaEmbedBridge.test.js` 与 `web/test/unit/oaEmbedSession.test.js` 覆盖导航通道的正向载荷与缺参拒绝；后端 `backend/test/unit/knowledge/test_source_references.py` 覆盖 `ecType` 写入。前端 lint、单测、构建与后端单测的实际结果记录在 PR。真实 OA 父插件是否实现 `new-navigate` 不由这些检查证明。
