# 主题配色系统与主色单一来源

状态：implemented
类型：feature
Owner：web/src/assets/css/base.css

## 问题

前端主色存在三份互相独立的真值：`stores/theme.js` 把 antd 的 `colorPrimary` 写死为 `#24839b`（且与 `base.css` 的 `--main-600: #24839a` 差一位字符，属抄写错误），`base.css` 的 `--main-color` 取 700 档，而 `base.dark.css` 又把 `--main-color` 改成另一个游离值 `#4a9fb8`、`--main-bright: #0188a6` 完全不在色阶刻度上。任一处漂移都会在「antd 按钮 vs 自研卡片」之间露出两种蓝，深色模式下 antd 更是固定不跟随。

同时色相职责重叠：`--color-accent-*`（氰 H180）与主色（青蓝 H191）只差 11°，强调失效；`--color-accent-*`、`--color-info-*`、`--color-warning-*` 三族与主色共同构成屏幕上的一组近似蓝/近似黄。

## 决策

主色改用项目 logo 取色的品牌蓝（`web/public/jiudian-pharma-logo.png` 飘带深蓝 H233），辅助色用 logo 金（H40），并统一约定：

- 色阶真值只有 `base.css` / `base.dark.css` 两份（浅深各一）。18 档按「固定饱和度曲线 + 按目标相对亮度反解明度」生成，不用 HSL 明度直接铺，因为 HSL 的 L 是几何明度、不是感知亮度（青色与蓝色同 L 下相对亮度可差 3 倍）。
- `--main-color: var(--main-600)`，浅深各取自各自色表；`--main-bright` 保留为「主色的高亮变体」槽位，浅色取 500 档、深色取 700 档（深色色表整体反转，更亮对应更大档号）。
- `theme.js` 不再写死主色，改为 `getComputedStyle` 读 `--main-color`；切换主题时先落 `:root.dark` 类、再重建 token，否则深色首屏会读到浅色值。
- 拆色相职责：`--color-warning-*` 色相从 40° 明黄移到 28° 橙（逐档保留原值的相对亮度与饱和度，只换色相，层级不变）；`--color-info-*` 挂到 `--main-*`；`--color-accent-*` 挂到 `--second-*`。
- 深色下 antd 的实心主按钮文字改为近黑：antd 的 `colorTextLightSolid` 恒取 `colorWhite`（`components/theme/util/alias.ts`），亮色主色底配纯白字只有 2.8:1。该 token 是全局 alias，改全局会连带把 Tooltip 的深底白字改黑，因此只覆盖 Button 组件（`formatToken` 中 `...overrideTokens` 在 aliasToken 之后展开，组件级 override 可覆盖）。
- 深色最底层抬亮：`--gray-0/10/25/50/100` 由 `#030303/#080808/#0c0d0d/#151616/#1e1f1f` 改为 `#151517/#171719/#19191b/#1b1b1c/#212123`。因为 `body` 的背景直接取 `--gray-0`，原值近似纯黑，整页观感压抑。改值不改名，保持「档号越小越深」的单调关系与原有档距。
- 深色下同一层背景不再存两份真值：写死的 `--bg-sider: #141414` 与 `--color-bg-container: #1f1f1f`、`--color-bg-elevated: #262626` 改为 `var(--gray-50)` / `var(--gray-50)` / `var(--gray-100)`，取值方式与浅色主题一致（浅色下本就是 `var(--main-5)` / `var(--main-0)` / `var(--gray-10)`）。
- antd 深色底必须同步覆盖：`theme.js` 的深色 token 新增 `colorBgBase` / `colorBgContainer` / `colorBgElevated`，取值同样由 `getComputedStyle` 从 `--gray-0` / `--gray-50` / `--gray-100` 读出，不外挂第三份真值。`darkAlgorithm` 默认从纯黑派生整套中性色（底色 `#000`、容器 `#141414`），不覆盖的话 antd 组件（卡片、表格、下拉、弹窗）会明显比自研区域更黑，同一屏上出现两种深色。

## 替代方案

- **继续维护 antd 的硬编码主色**：需要每次改色两处同步，本次问题正是由此产生，不采纳。
- **在 token 里直接写 `var(--main-color)`**：antd 要基于 `colorPrimary` 派生 hover/active/浅底等一整套颜色，收到 `var()` 字符串无法参与颜色运算，会算出一批无效色值，不采纳。
- **深色下把 antd 的 `colorPrimary` 降到能与白字达标的档位**：仍属同一 token 兼任「实心底」与「深底上的强调文字」两个互相拉扯的角色，降档会把菜单选中、Tabs 激活等文字一起压暗，只是把冲突搬进 antd 内部。
- **重算 `--gray-*`（17 档独立命名）使其偏向新主色**：整套灰阶饱和度仅 3~6%，换色后偏差在 1~2 个 8 位色阶内，肉眼不可辨；改名或改值会波及全站上百处引用，收益不抵风险，本轮不动。
- **同步改 `--color-success-*` / `--color-error-*`**：与主色、辅助色均无冲突，且状态色是最依赖用户条件反射识别的一套，不为统一而改。
- **深色只改自研 CSS 变量、不动 antd token**：不采纳。antd 组件（下拉、弹窗、表格、卡片）在深色页面里占比很大，只抬亮自研区域会让同屏出现两种深浅不同的深色，比不改更明显。
- **改 antd 的 `colorBgBase` 但不钉 `colorBgContainer` / `colorBgElevated`**：不采纳。虽然 `colorBgBase` 是 seed token、会连带重新派生整套中性色（这正是想要的效果），但派生结果无法保证与自研灰阶的档位一一对上，容器与浮层显式钉住才能让 antd 卡片与自研卡片同色。
- **抬亮深色背景时连带抬 `--main-5 ~ --main-30`**：本轮不做。`--main-0` 同时承担「压在主色实底上的文字色」角色（自研按钮 `color: var(--gray-0)` / antd 实心按钮近黑字同理），抬亮会削弱该对比度；要两者兼得需先把这两个角色拆成不同 token，属另一项裁决。当前保留品牌色极深档略暗于中性底的现状，作深底时呈轻微凹陷观感。

## 后果

- 主色的唯一真值是 `base.css` / `base.dark.css` 的 `--main-600`；antd 组件与自研样式的交互色从此同源，深色模式自动跟随。
- 装饰金与告警色不再是同一色相区间；屏幕上不再出现第二、第三种近似蓝。
- 7 个文件夹图标的写死旧主色、`QuerySection` 的旧主色阴影、`TodoListTool` 的写死状态色、`chartColors.js` 的兜底色值同步更新，消除了换色后残留的青色。
- 深色主按钮文字改为近黑，与自研按钮（`--main-color` 配 `--gray-0`）的做法一致；这是 Material 3 类深色模式的常规做法，代价是 antd 深色实心按钮的文字不再统一为白。
- 深色下 `--main-5 ~ --main-30`（`#0c0e15 ~ #1d203a`）为品牌色极深档，抬亮中性底后比新页面底（`#151517`）更暗，作背景用时呈轻微凹陷观感；本轮未同步抬亮，原因见替代方案。如需与中性底严格对齐，需先把「极深背景」与「压在主色实底上的文字色」两个角色拆成不同 token。
- 深色页面底由 `#030303` 抬到 `#151517`、卡片层由 `#1f1f1f` 抬到 `#1b1b1c`，自研样式与 antd 组件共用同一组灰阶。两者对比度约 1.06:1，卡片与页面底主要靠描边与阴影区分 —— 这是所选色值的直接结果；若需要更明显的层次，把 `--gray-50` 提到 `#1f1f21` 即可（改一处，两套组件同时生效）。
- `ContextUsageRing` 的 `--error-color` 兜底引用一并与 `--warning-color` 对齐，改为 `var(--color-error-500)`：该变量全仓未定义，原先一直靠兜底值生效。

## 验证

- 色阶数据本身可核对：两份样式文件里的 18 档刻度满足单调性（相对亮度随档号单调），浅色 `--main-600` 配白字对比度 4.89:1、深色 3.0:1（故深色实心按钮改用近黑字）。
- 变量引用完整性：在 `web/src` 下扫描 `.vue` / `.js` / `.css` / `.less`，统计「被 `var(--x)` 引用」与「全仓有 `--x:` 定义」两个集合。改动后被引用 140 个变量，其中 30 个全仓未定义（`--color-error-600`、`--gray-250`、`--main-25`、`--text-primary` 等），全部是存量问题且引用处都带兜底值；本次改动**没有新增**未定义项，也没有留下悬空引用。
- `node --check` 通过改动的 `web/src/stores/theme.js` 与 `web/src/utils/chartColors.js`。
- 前端 lint / 单测 / 生产构建由提交后的 CI 执行并通过（`.github/workflows/web.yml` 的 `read-only web gates`：`pnpm run lint:check && pnpm run test:unit && pnpm run build`）。这条同时也证明 `.vue` 内联样式块能正常编译。
- `python3 scripts/verify_engineering_contracts.py` 通过（含本决策记录的格式校验）。
- 视觉确认（人工判断，无可复现命令）：把改动后的两份样式文件按 `<link>` 引入一个本地静态页面，渲染侧边栏 / 顶栏 / 卡片 / 标签 / 按钮 / 气泡 / 状态芯片的实景并截图，覆盖浅深两种模式、语义色矩阵与「旧值 vs 新值」并排对照，由作者逐张目视确认层级与可读性。该页面与截图属本地草稿（`docs/vibe/` 已被 gitignore），未随本次改动入库。
- 未验证范围：`theme.js` 的组件级 Button override 只做了源码级确认，未在真实 antd 运行时断言深色主按钮文字色；未在真实浏览器中走查全站页面的实际观感（前端产物能编译、能过单测，不等于每个页面的观感都符合预期）。
