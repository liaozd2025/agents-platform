# 知识图谱三维星系渲染

状态：implemented
类型：feature
Owner：web/src/components/GraphCanvas.vue

本记录取代并延续历史记录：[2026-08-28-knowledge-graph-node-visuals.md](../archived/2026-08-28-knowledge-graph-node-visuals.md)。

## 问题

当前知识图谱虽可展示关系，但仍是二维平面，无法提供可旋转的空间层次、景深和星系式探索体验。改造不得改变既有图数据接口、搜索、详情抽屉和 `GraphCanvas` 对外事件。

## 决策

引入 `3d-force-graph` 作为 `GraphCanvas.vue` 的渲染内核。使用其 Three.js 场景和三维力导向布局，提供可拖拽旋转、缩放、发光节点、冷色关系线和沿边流动的粒子。

节点按实体类型稳定配色，大小由连接度和引用影响力派生；选中或悬浮时突出目标节点及一跳邻居。组件继续发出节点、边和空白画布点击事件，并继续暴露刷新、聚焦、清除聚焦和关键词高亮方法。

## 替代方案

继续使用 Sigma.js：Sigma 是高性能 WebGL 二维图渲染器，不能提供可旋转的真三维场景。

使用 `react-force-graph`：当前项目为 Vue 3，引入 React 运行时和跨框架状态同步没有必要。

使用纯 CSS 或 Canvas 模拟三维：不能提供真实空间坐标、景深和旋转交互。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 图谱可在真实 Three.js 场景中旋转和缩放 | 仍渲染为二维或容器为空 | web/src/components/GraphCanvas.vue | 依赖安装、生产构建和本地页面 HTTP 200 | 空数据不创建无效节点或边 | Passed（生产构建）；真实 WebGL 截图未执行 |
| 既有图谱调用契约保持可用 | 点击、搜索或详情抽屉失效 | web/src/components/GraphCanvas.vue、KnowledgeGraphSection.vue | lint、单元测试和生产构建 | 不存在的节点聚焦不应抛错 | Passed（lint/构建）；单测有 2 个无关既有失败 |
| 白色图谱画布中的节点、边和标签可读 | 节点、边或标签与背景不可辨识 | web/src/components/GraphCanvas.vue | 配色代码审查和生产构建 | 清除聚焦后恢复全图可见 | Inspected；真实浏览器截图未执行 |

## 后果

Three.js 场景会增加浏览器 GPU 开销，且大图谱可能降低帧率。实现需要在组件卸载和数据更新时销毁旧实例，防止渲染循环、事件监听和 WebGL 资源累积；真实知识库数据下的性能需要单独页面验收。
