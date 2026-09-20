# 项目目录选择器为托管会话目录显示可读名称

状态：implemented
类型：feature
Owner：web/src/utils/projectSelection.js

## 问题

新建项目弹窗的项目目录选择器直接展示个人空间条目名。服务端托管的会话目录名就是 Project 的 uuid（implicit Project 的 `name` 为 `null`，目录取 `projects/<project_id>`），历史数据由 v071 迁移按 `md5(uid:线程id)` 派生。用户在 `/projects` 下看到两串裸 uuid，无法判断目录来源，也无法判断选择后复用哪次会话的工作区。对话页右侧「个人空间」文件树同样列出这些 uuid 目录，且能否看到它们取决于是否恰好有 Project 绑定在 `projects` 根上，用户同样无法辨识。

## 决策

前端在展示层为托管会话目录补一个可读名称，服务端目录名与数据库事实保持不变：

- `web/src/utils/projectSelection.js` 新增 `buildDirectoryLabels` 与 `resolveDirectoryLabel`。名称来源优先级为「已命名 Project 的名称 > 该目录最近会话标题 > 兜底文案『历史会话目录』」。
- 仅处理 `/projects/<uuid>/` 这一层；`projects` 根、更深层级以及 `projects` 之外的目录沿用服务端原始名称。
- 名称来源集中在 `web/src/stores/projects.js`：`ensureDirectoryLabels` 一次拉取项目与 `/api/projects/history-candidates?limit=100` 并缓存，并发的入口共享同一请求；`resolveDirectoryLabel` 暴露给各入口。请求失败时映射为空，各入口回退为原始目录名，浏览与选目录不受影响。
- `WorkspacePathPicker` 新增可选 `resolveDirectoryLabel` 函数 prop：命中时目录行以「可读名称（原始目录名）」单行呈现。新建项目弹窗、添加文件弹窗与产物保存弹窗都注入该解析器，未注入的调用点行为不变。
- `AgentPanel` 的个人空间文件树用同一映射，目录节点命中时以「可读名称（原始目录名）」单行呈现；该树加载目录时显式请求未绑定的项目目录，使每个会话目录都能被辨认，不再依赖「是否有 Project 绑定在 `projects` 根」这一偶然条件。
- `WorkspaceView`（个人空间页）在加载目录前先取同一份名称来源，给托管会话目录补 `title` 与 `displaySuffix` 两个展示字段，`WorkspaceFileList` 的名称列把它们渲染成「可读名称（原始目录名）」；`name`、`path` 与其余列保持原值，进入目录、排序与图标仍按原始字段工作。该页同样显式请求未绑定的项目目录。

## 替代方案

在 `GET /api/workspace/tree`（`include_unbound_project_dirs=true`）为 `projects/*` 子目录补 `label`、`kind` 字段，可让文件管理器等所有入口共享同一份名称事实，代价是后端 service、响应契约、测试与决策记录一并变更并需要发版；本次要解决的是选目录场景的辨识问题，未采纳。把未命名目录统一改文案为「会话目录」无法回答“选哪个”，只消除乱码观感，作为兜底文案并入本次决策。在服务端为 implicit Project 回填用户可编辑名称会把展示需求变成持久数据与迁移问题，未采纳。

## 后果

目录名、`workdir_path` 与服务端返回结构均未改变，上传弹窗与产物卡片仍按原始路径与原始名称工作。名称来源受 `history-candidates` 的 100 条上限影响：项目数与会话数超出窗口时，部分目录回退到兜底文案。会话标题取自最近一条 active 会话，标题变化在下一次打开弹窗或重新进入个人空间时生效。取名请求失败时名称缺失，目录浏览与选目录本身不受影响。个人空间文件树改为包含未绑定的项目目录，`projects` 下会按会话数量列出目录，条目数量随历史会话增长；该树的写操作只有下载，展示范围扩大不引入误删风险。

## 验证

单元测试 `web/test/unit/projectSelection.test.js` 覆盖：项目名覆盖同目录的会话标题、无名称来源时兜底为「历史会话目录」、`Desktop`、`/notes`、`projects/<uuid>/reports` 与 `projects` 根保持原样，以及目录选择器按展示名渲染可读名称并保留原始目录名、三个注入解析器的弹窗入口各自触发名称加载。负向验证把两段赋值的先后顺序反转，测试「优先展示项目名」按预期失败，还原后通过。

本地执行结果：`node --test test/unit/projectSelection.test.js test/unit/workspace_action_semantics.test.js` 12 项全部通过；`test/unit/projectsStore.test.js` 2 项通过；`eslint --max-warnings=0` 覆盖 8 个改动源文件与测试文件、无输出；`vite build --outDir $TEMP/...` 构建成功。全量 `pnpm run test:unit` 为 275 通过 / 6 失败，失败集中在 `api_boundary.test.js`、`projectsStore.test.js`、`agentPanelSections.test.js`、`extension_detail_layout_runtime.test.js` 这类通过 `vite` 建立真实服务器的用例，运行时沙箱拒绝写入 `node_modules/.vite`、`.vite-temp` 预构建缓存；`projectsStore` 在沙箱放行的一次单跑中通过，这些用例也不导入本次改动的模块。

真实页面验证：本地站点 `localhost:5173`（dev 容器 bind mount `web/src`，改动经 HMR 生效），用后端签发的本地 token 以 `u2024102811` 登录。对话页右侧「文件」→「个人空间」→ 展开 `projects`，树中显示「请求生成示例文件查看效果（36acc7c9-4098-427b-8b56-0209545d83c3）」「询问用户自身身份（548f7838-d284-4d5e-b977-188f55cca0d4）」；个人空间页进入 `projects` 后名称列显示同样两行文本，uuid 为灰色小字。两处均与库中会话标题逐条一致，映射收进 store 后又复验一次仍一致。验证脚本为 `.workbuddy/verify-agentpanel-screenshot.py` 与 `.workbuddy/verify-workspace-screenshot.py`，需在无沙箱隔离下运行（沙箱内 `page.goto` 会超时）。添加文件弹窗与产物保存弹窗复用同一组件与同一 prop，其注入由单元测试断言守卫；这次没有为这两个入口单独截图。
