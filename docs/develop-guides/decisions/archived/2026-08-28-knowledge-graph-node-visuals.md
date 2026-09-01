# 知识图谱 Sigma 渲染与科幻视觉

状态：archived
类型：feature
Owner：web/src/components/GraphCanvas.vue

本记录已由 [2026-08-31-knowledge-graph-3d-rendering.md](../implemented/2026-08-31-knowledge-graph-3d-rendering.md) 取代，仅保留 Sigma 阶段的历史决策。

## 问题

现有 G6 图谱的交互和视觉效果不符合知识关系网的展示需求。页面需要保留既有的数据接口、搜索、详情抽屉和组件事件契约，同时提供更明显的冷色科技感、关系聚焦和稳定布局。

## 决策

使用项目已安装的 Sigma.js 和 Graphology 替换 G6，渲染 Owner 仍为 `web/src/components/GraphCanvas.vue`。

节点按实体类型使用稳定配色，Chunk 节点保持灰色；节点尺寸由连接度和引用影响力派生。深色主题使用深蓝黑底、细网格、冷色节点和连线；浅色主题保持低干扰网格。点击或悬浮节点时，只强化该节点及一跳邻居，其余节点和边弱化。初始位置采用确定性黄金角分布，避免刷新时随机跳动。

## 替代方案

不采用 `react-force-graph`：项目是 Vue 3，强行接入 React 会引入额外运行时、状态同步和维护成本。

不采用 Vue Flow：它面向固定流程图，不能自然表达自动布局的知识关系网。

不新增依赖：Sigma.js 与 Graphology 已在项目依赖中锁定。

## 后果

图谱的上游接口、搜索、详情抽屉和 `GraphCanvas` 对外事件、方法保持不变；渲染实现由 G6 切换为 Sigma。渲染器在数据和主题变化时重建，避免旧事件与图形资源累积。

大规模图谱的真实页面帧率和复杂关系可读性仍需要用实际知识库数据验收。

## 验证

- `docker compose exec web pnpm run lint:check`：Passed
- `docker compose exec web pnpm run build`：Passed
- `git diff --check`：Passed
- 真实页面截图和交互验收：未执行
