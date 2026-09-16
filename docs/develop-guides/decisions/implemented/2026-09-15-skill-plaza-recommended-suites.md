# 技能广场推荐套件扩展

状态：implemented
类型：feature
Owner：`web/src/components/extensions/SkillCardList.vue`（`RECOMMENDED_SUITES` 是推荐列表的唯一事实源）

## 问题

技能广场的推荐列表原本只有「MiniMax 办公文档套件」和「Skill 能力与进化套件」两组，覆盖不到演示文稿生成、翻译、研究分析和办公自动化这些高频需求。推荐列表是前端随代码发布的资产，远程安装能力已经由 `backend/server/routers/skill_router.py` 的 `remote/list|search|prepare` 与 `yuxi.agents.skills.remote_install` 提供，缺的只是推荐内容。

首版扩充了 5 组套件，但其中 4 组实际装不上。在真实沙箱（`_RemoteSkillSandbox`）中复现后确认根因：**沙箱出网到 `github.com:443` 严重抖动**（连续 3 次探测为 timeout / timeout / 200，耗时 7.8s），而 `npx skills add` 内部走 `git clone https://github.com/<owner>/<repo>`，必须打 `github.com`；同一批 `prepare_remote_skills_batch` 里有的组成功、有的组失败，就是抖动造成的。仓库越大越容易失败：`claude-office-skills/skills` 约 854KB，`jimliu/baoyu-skills` 拉取 90 秒仍未完成（已 14.7MB）。失败时后端只会返回统一的 `CLI 安装失败`，真实 CLI 报错被吞掉。

## 决策

保留既有逻辑与安装链路不变，**只调整 `RECOMMENDED_SUITES` 的数据**：把 4 组装不上的套件改为按技能指定白名单内的 `modelscope.cn` 镜像来源（`https://modelscope.cn/skills/@<owner>/<skill>`），因为这些镜像在沙箱内可稳定访问、逐技能实测全部安装成功。

最终 7 组套件 / 27 个 skill：

| 套件 | category | 来源 | skill |
| --- | --- | --- | --- |
| MiniMax 办公文档套件 | creation | `modelscope.cn/collections/MiniMax/MiniMax-Office-skills` | pptx-generator、minimax-docx、minimax-xlsx、minimax-pdf |
| Skill 能力与进化套件 | productivity | 逐技能 `modelscope.cn/skills/@...` + `zhaono1/agent-playbook` | skill-creator、find-skills、self-improving-agent |
| Anthropic 官方文档套件 | creation | `https://github.com/anthropics/skills` | pptx、docx、xlsx、pdf |
| 演示文稿与视觉套件 | creation | `modelscope.cn/skills/@...` | presentation-content、presentation-design、presentation-pitch-deck、presentation-creator |
| 翻译与写作套件 | creation | 3 个魔搭镜像 + `github.com/wshuyi/translate-pdf-skill` | baoyu-translate、translate-pdf、translation、mkdocs-translations |
| 研究与竞品分析套件 | research | `modelscope.cn/skills/@...` | account-research、research-synthesis、persona-researcher、status-report |
| 办公自动化套件 | productivity | `modelscope.cn/skills/@...` | wps-excel、excel-automation、docx-manipulation、pdf-to-docx |

套件的 `source` 只在技能自身没有 `source` 时兜底（`SkillInstallFlowModal.prepareSuite`），因此同一套件内可以混合多个来源；本次 4 个新套件的每个 skill 都带自己的 `source`，不再设置套件级 `source`。

单一来源的组仍整组安装：`prepareSuite` 按 `source` 分组后逐组调用 `remote/prepare`，一组失败只影响该组。

### 广场卡片启停覆盖个人技能

套件安装默认落到个人来源（`installTarget` 首选项为 `personal`），而这些卡片在广场列表里**没有启停入口**，与共享技能卡不一致。原因是模板把个人技能排除了：

```
v-if="skill.sourceScope !== 'personal' && canManageSkill(skill)"          // 卡片按钮
v-if="previewSkill.sourceScope !== 'personal' && canManageSkill(previewSkill)"  // 预览开关
```

但权限助手 `canManageSkill` 本身已经按作用域分流（个人看 `skill:use`、共享看 `skill:manage`），后端也已有 `PUT /api/skills/personal/{slug}/enabled`（`update_personal_skill_enabled` + `ResolvedSkill.enabled` 持久化在个人 Skill 目录内）。所以决策是**去掉模板里的作用域排除，不改权限助手**，并让 `handleToggleSkillEnabled` 按作用域分流到对应接口：

- 个人 → `skillApi.updatePersonalSkillEnabled`
- 共享 → `skillApi.updateSkillEnabled`
- 个人与共享允许同名（`list_skill_cards_for_user` 用 `overrides_shared` 表达同名关系），因此定位 `skills.value` 卡片时补上作用域条件，避免改到另一个版本；预览对象同步也加了同一条件。

### 套件弹窗内已安装技能整行可切换

套件弹窗（`SkillInstallFlowModal` 的 `selecting` 阶段）里，已安装技能虽然右侧有启停按钮，但左侧勾选框被 `:disabled="isInstalled(...)"` 锁死、整行也不可点，`cursor: not-allowed` 让用户自然去点勾选框或整行——点击必然无反应。权限与后端都不阻塞：`canToggleSkill` 通过、`PUT /api/skills/personal/{slug}/enabled` 实测可写（诊断脚本对 `wps-excel` 停用/启用往返成功）。

因此决策是把已安装行**从"禁用勾选框的 `<label>`"改成"整行可点的切换按钮"**，未安装行保持原勾选安装入口：

- 已安装 → `<button class="selection-item installed installed-toggle">`，整行点击即切换，左侧状态图标 + 右侧"已启用/已停用"文案，切换中显示 spinner。
- `disabled` 只绑定 `isSkillToggling`，不再绑定权限：无权限、找不到安装记录、请求失败一律走 `message.warning/error` 可见反馈，避免再次出现"点了没反应"的死按钮。
- 已安装记录的解析按 `slug` 或 `name` 双路兜底（`resolveInstalledSkill`），套件定义与安装记录标识不一致时不会退化成死按钮。
- 切换成功后 `emit('skills-changed')`，父组件 `SkillCardList` 重新拉取列表，广场套件卡与"我的技能"的启用/停用状态随之刷新。
- 切换成功后的提示统一为 `Skill 已启用` / `Skill 已禁用`，不拼接 Skill 名称，与广场卡片 `handleToggleSkillEnabled` 的文案保持一致；行内的"已启用/已停用"状态文案仍保留，用于区分当前状态。
- **目标状态必须由有效状态推导**：切换目标写作 `const enabled = isSkillDisabled(skill)`，而不是读 `installed.enabled`。后者是弹窗打开时从 `props.flow.installedSkills` 拿到的快照，弹窗内不会更新，也不随父组件重新拉取而变化；用它计算目标会让第二次及以后的点击提交与第一次相同的值，后端幂等返回、界面不再变化，表现为"改一次之后卡死，必须退出弹窗重进"。`isSkillDisabled` 会把本地 `skillEnabledOverrides` 算进来，因此连续点击能正确往返。这一条同时意味着弹窗内的启用状态真相是覆盖值 + 快照的组合，父组件不必回写 `installFlow`（回写还会因 `watch([props.open, props.flow])` 重置覆盖值）。

### 新增「中文去 AI 味套件」

用户要求把「去 AI 味」这类技能放进广场供所有人安装。广场推荐位是前端常量，每个 skill 的 `source` 必须是白名单内的远程地址，因此只能从公开仓库取源，不能靠上传 zip（zip 走的是另一条个人/共享安装链路，进不了推荐位）。

在 skills registry 里检索「去 AI 味」得到多个开源实现（安装量从高到低：`zenstory-ai/oh-story-claudecode@story-deslop`、`ai-zixun/humanizer-zh@humanizer-zh`、`lifelonglazylearner/qu-ai-wei@qu-ai-wei`、`chujianyun/skills@remove-ai-flavor`）。选定 `ai-zixun/humanizer-zh`，理由：

- 仓库存在且根目录直接是 `SKILL.md`（`name: humanizer-zh`），符合 `npx skills add <repo> --skill humanizer-zh` 的目录约定；
- 内容对口——它在 frontmatter 里显式声明了「去 AI 味」「润色成中文母语表达」「减少翻译腔」等触发语，是中文场景的完整实现，而非英文 humanizer 的直译；
- 走 `github.com` 原仓库而不是 `modelscope.cn` 镜像，是因为当时无法确认该 skill 存在对应的魔搭镜像地址（`@ai-zixun/humanizer-zh`），而 GitHub 仓库本身可直接核验存在。

新增独立的 `de-ai-writing-suite` 套件（1 个 skill）而不是并入「翻译与写作套件」，是为了让这张能力卡在广场里可直接被识别，不需要先打开翻译套件才发现。

## 替代方案

- **改后端把 GitHub 来源换成走 `codeload.github.com` / `api.github.com` 的 tarball**：实测这两个域名在沙箱内稳定可达（均 200，能取到完整 tarball），能根治；但需要新写取源实现、替换第三方 CLI、补单测与负向用例，改动面远大于本次问题。
- **保留 GitHub 来源并加重试**：抖动的观测失败率约 2/3，重试 3 次成功率约 96%，但每次失败要等 20–30 秒，仍会偶发失败，且掩盖真实原因。
- **在部署侧给沙箱注入 HTTPS 代理或放通 `github.com:443`**：`_RemoteSkillSandbox` 用 `inherit_env=False` 创建，需要在 provisioning 层注入，属部署方职责，仓库内无法保证；线上出网正常时该问题自然消失。
- **整体改为 ModelScope 合集来源**：集合面比逐技能镜像窄，`/collections/.../.well-known/...` 只对少数合集存在。
- **把推荐列表搬到后端配置或数据库**：可以不发版调整推荐，但引入新的持久状态、管理界面、缓存失效与权限面，与本次问题无关。
- **内置用户个人技能 `ppt-forge`、`translator`**：正文引用了工作区中不存在的外部文件，内置后 Agent 会读到悬空引用，不采用。

## 后果

- 用户可在技能广场按套件勾选 skill 安装到个人或共享来源，复用既有 `remote/prepare` → 草稿 → 确认链路，后端与数据库零改动。
- 套件弹窗复用单个 Skill 的对勾/添加开关；个人 Skill 将启用状态持久化在其目录内，停用后不再投影到 Agent 运行时，同名共享版本可重新生效。
- 广场列表的个人技能卡片与预览弹窗现在也有启停入口，与共享技能行为一致；两者共用 `handleToggleSkillEnabled`，只是存储接口按作用域分流。
- 套件弹窗的已安装行整行可点切换，未安装行仍是勾选安装，两类交互在视觉上可区分（左侧状态图标 / 勾选框 + 右侧"已启用/已停用"文案）。
- 推荐列表仍是前端硬编码，调整内容需要重新发版。
- 4 个新套件走 `modelscope.cn`，在沙箱内实测稳定；`Anthropic 官方文档套件` 与 `translate-pdf` 仍走 `github.com`，在网络受限环境下仍可能失败（`anthropics/skills` 实测 4/4 两轮通过，耗时 46–57s，明显慢于镜像来源的 18–28s）。
- 魔搭镜像的技能与上游内容一致：`claude-office-skills` 的技能本身就是单文件（只有 `SKILL.md`），镜像不含额外脚本/引用文件，无内容损失；`@claude-office-skills/excel-automation` 与 `@claude-office-skills/pptx-manipulation` 的 `description` 在**上游源里本来就是损坏的**（YAML 里写成 `>`，解析结果为 `大于`），广场卡片使用自定义文案展示，但安装后 Agent 读到的前置描述仍是坏的，自动触发会受限。
- `baoyu-translate` 自带 `scripts/`（`bun.lock`、`main.ts`），需要 `bun` 或 `npx` 作为运行时。

## 验证

- **真实沙箱安装（决定性证据）**：在运行中的 api 容器内直接调用 `prepare_remote_skills_batch`，走沙箱内 `npx skills add`，逐技能实测：演示文稿与视觉 4/4、翻译与写作 3/3、研究与竞品分析 4/4、办公自动化 5/5，**共 16/16 全部成功**，单次耗时 17.6–42.2s。对照组：`modelscope.cn/collections/MiniMax/...` 两轮均 4/4、耗时恒为 27.7s；`anthropics/skills` 两轮均 4/4（56.5s / 46.0s）。
- **失败复现与根因**：沙箱内 `curl` 出网矩阵为 `github.com` 超时（3 次中 2 次失败）、`codeload.github.com` 200、`api.github.com` 200、`modelscope.cn` 302、`registry.npmjs.org` 200、`skills.sh` 308；`git ls-remote github.com` 报 `Failed to connect ... Connection refused`；CLI 报 `Failed to clone repository`。
- **结构与数据**：node 解析 `RECOMMENDED_SUITES`，得到 7 套件 / 27 技能，字段齐全，无重复 id、无跨套件重复 slug，所有 `source` 命中白名单前缀（`github.com` / `modelscope.cn`）。
- **编译**：`docker compose exec -T web pnpm run lint:check` 通过；向运行中的 Vite dev server 请求该 SFC 返回 200，产物中 4 个新套件名各出现 1 次，旧来源字符串 0 次，无 transform 错误。
- **单测**：**先同步宿主机的测试集**——`docker-compose.yml` 的 web 服务只挂载 `web/src`、`web/public`、`index.html`、`vite.config.js`，**不挂载 `web/test`**，所以容器内直接跑 `pnpm run test:unit` 用的是镜像里烘焙的旧测试副本。执行 `docker compose cp web/test/. web:/app/test/` 后再跑，结果为 **262 项 / 261 通过 / 1 失败**；唯一失败项是 `agentPanelSections.test.js:143`（读取 `AgentPanel.vue`，零处引用本组件），与本改动无关。
- **更正上一版记录**：先前记录的「229 项 / 220 通过 / 9 失败、`skillAndSourceMerge.test.js` 通过」是旧测试副本下的结果，且该文件当时并未在容器内运行。用宿主机测试集运行时，该文件因 `plazaGroups` 在本轮新增了对 `installedSkillCards`、`isSystemSkill` 的依赖而报 `ReferenceError`（测试的注入参数未同步），已显式补上这两个注入项，断言语义未改。
- **新增守护测试与负向验证**：`test/unit/skillAndSourceMerge.test.js` 新增用例断言卡片按钮与预览开关不再按作用域排除个人技能，且启停按作用域分流。把缺陷恢复（重新写入 `sourceScope !== 'personal' && canManageSkill(...)`）后该用例失败（2 通过 / 1 失败），移除后恢复 3/3 通过。
- **后端个人启停往返实测**：在 api 容器内直接调用 `update_personal_skill_enabled('gzrgzr', 'wps-excel', enabled=False/True)`，两次返回值 `enabled` 与入参一致，状态写入个人 Skill 目录的 `.yuxi-skill-state.json`，佐证"点不动"不是后端问题（诊断后已把该技能还原为停用）。
- **套件行切换的守护测试**：` skillAndSourceMerge.test.js` 新增用例断言已安装行为 `installed-toggle` 整行按钮、`@click="toggleSkillEnabled(skill)"`、不再有 `:disabled="isInstalled(skill.slug)"`、失败走 `message.error`、缺记录走 `message.warning`、父组件 `@skills-changed` 触发重新拉取；同时断言未安装行仍保留 `<label v-if="!isInstalled(...)">` 勾选入口。该文件 4/4 通过，eslint `--max-warnings=0` 通过，并通过运行中的 Vite dev server 请求该 SFC 确认无 transform 错误、产物含 `installed-toggle`。
- **连续切换卡死的守护测试与负向验证**：`test/unit/skillAndSourceMerge.test.js` 增加两条断言——目标状态必须是 `const enabled = isSkillDisabled(skill)`，且不得再出现 `const enabled = installed.enabled === false`。负向验证用一次性脚本把缺陷行注回源码文本，该 `doesNotMatch` 断言按预期抛出（`negative check ok`）；移除后 4/4 通过，eslint `--max-warnings=0` 通过，Vite dev server 返回 200 且产物内 `installed.enabled === false` 出现 0 次。
- **文案去名称的守护测试与负向验证**：同文件另加两条断言——成功提示必须是 `` message.success(`Skill 已${nextEnabled ? '启用' : '禁用'}`) ``，且不得再出现 `` message.success(`已${nextEnabled`` 形式。把含名称的旧文案注回源码后该 `doesNotMatch` 断言按预期抛出，移除后 4/4 通过；eslint `--max-warnings=0` 通过，Vite dev server 返回 200 且产物第 237 行为新文案。
- **新增白名单守护测试 + 去 AI 味条目**：`skillAndSourceMerge.test.js` 增加用例，从 `SkillCardList.vue` 源码里解析 `RECOMMENDED_SUITES` 区块的所有 `source`，断言其 hostname 全在 `github.com` / `modelscope.cn` 内，并断言 `humanizer-zh` 已在列表；该文件 5/5 通过，eslint `--max-warnings=0` 通过。
- **去 AI 味来源核验（仅存在性）**：通过 GitHub API 确认 `ai-zixun/humanizer-zh` 仓库存在、根目录含 `SKILL.md`，再读 `SKILL.md` 的 frontmatter，确认 `name: humanizer-zh` 与声明的中文「去 AI 味」触发语。
- **未验证（去 AI 味技能）**：未在真实沙箱内完成一次 `npx skills add https://github.com/ai-zixun/humanizer-zh --skill humanizer-zh`。探测时 `modelscope.cn` 的两个候选均在 `--list` 阶段 `ReadTimeout`（沙箱出网抖动，与来源本身无关），GitHub 候选未跑完即按用户要求中止。因此该条目"能列在广场、能点安装"已由代码与单测证明，"能否真的下到文件"仍取决于部署机容器的出网，未实测。
- **未验证**：没有在已登录的真实页面点开安装流程并完成一次端到端远程安装（本地无可用登录凭据，登录走 OIDC）；套件弹窗内"点击已安装行切换启停"同样只做到 lint、单测与 Vite 真实编译（SFC 返回 200、无 transform 错误、产物内含 `installed-toggle`），未在已登录页面点击一次并回读 `enabled` 落库结果。镜像来源依赖 `modelscope.cn` 在部署环境的可达性，未在线上环境验证。
