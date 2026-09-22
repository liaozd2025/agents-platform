import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed } from 'vue'

const source = (path) => readFileSync(new URL(`../../src/${path}`, import.meta.url), 'utf8')

test('技能广场同时应用搜索和分类，不匹配时显示空列表', () => {
  const text = source('components/extensions/SkillCardList.vue')
  const expression = text.slice(
    text.indexOf('const plazaGroups ='),
    text.indexOf('const mineSkills =')
  )
  // plazaGroups 同时消费广场套件卡与内置/共享技能卡，个人技能不进入广场浏览区。
  const evaluate = new Function(
    'computed',
    'SKILL_CATEGORIES',
    'recommendedSuiteCards',
    'installedSkillCards',
    'matchesSearch',
    'skillCategory',
    `${expression}; return plazaGroups.value`
  )
  const categories = [{ key: 'all', label: '全部' }, { key: 'creation', label: '创作' }]
  const cards = { value: [{ name: 'PDF', category: 'creation' }, { name: 'Search', category: 'research' }] }
  // 已安装列表里的共享/个人 Skill 只在广场浏览区保留共享那条。
  const installedCards = {
    value: [
      { name: 'Shared', category: 'creation', sourceScope: 'shared' },
      { name: 'Personal', category: 'creation', sourceScope: 'personal' }
    ]
  }
  const run = (matchesSearch) =>
    evaluate(computed, categories, cards, installedCards, matchesSearch, (s) => s.category)
  assert.deepEqual(run(() => false), [])
  const groups = run((s) => s.name === 'PDF')
  assert.deepEqual(groups.map((g) => g.skills.map((s) => s.name)), [['PDF'], ['PDF']])
  const shared = run((s) => s.name === 'Shared')
  assert.deepEqual(shared.map((g) => g.skills.map((s) => s.name)), [['Shared'], ['Shared']])
  // 个人 Skill 不允许出现在广场浏览区
  assert.deepEqual(run((s) => s.name === 'Personal'), [])
})

test('技能广场单技能的「立即使用」覆盖共享/内置技能，停用时不可用', () => {
  const text = source('components/extensions/SkillCardList.vue')
  const expression = text.slice(
    text.indexOf('const canUsePreviewSkill ='),
    text.indexOf('// 仓库拉取的技能列表过滤')
  )
  const evaluate = new Function(
    'computed',
    'previewSkill',
    `${expression}; return canUsePreviewSkill.value`
  )
  const run = (skill) => evaluate(computed, { value: skill })

  // 广场浏览区的共享/内置单技能同样提供立即使用入口
  assert.equal(run({ slug: 'mysql-reporter', sourceScope: 'shared', enabled: true }), true)
  assert.equal(run({ slug: 'knowledge-base', sourceScope: 'builtin', enabled: true }), true)
  // 个人技能（含广场下载与自行上传）保持可用
  assert.equal(run({ slug: 'translator', sourceScope: 'personal', enabled: true }), true)
  // 停用的技能不可跳转使用（按钮置灰），未选中预览目标时同样不可用
  assert.equal(run({ slug: 'translator', sourceScope: 'personal', enabled: false }), false)
  assert.equal(run({ slug: 'mysql-reporter', sourceScope: 'shared', enabled: false }), false)
  assert.equal(run(null), false)

  // 按钮始终渲染，靠 disabled 置灰而不是隐藏，避免「按了没反应」的歧义
  assert.match(text, /:disabled="!canUsePreviewSkill"/)
  assert.doesNotMatch(text, /<a-button\s+v-if="canUsePreviewSkill"/)
  // 处理函数自身也要挡一层，防止其它入口绕过置灰直接跳转
  assert.match(
    text,
    /const usePreviewSkillInChat = \(\) => \{[\s\S]*?if \(!canUsePreviewSkill\.value\) return/
  )
})

test('技能广场卡片启停覆盖个人技能并分流到个人接口', () => {
  const text = source('components/extensions/SkillCardList.vue')
  // 卡片启停按钮与预览开关都不得再按作用域把个人技能排除在外
  assert.doesNotMatch(text, /sourceScope !== 'personal' && canManageSkill\(skill\)/)
  assert.doesNotMatch(text, /sourceScope !== 'personal' && canManageSkill\(previewSkill\)/)
  // 个人与共享 Skill 走不同接口，且按作用域定位卡片
  assert.match(text, /const isPersonal = skill\.sourceScope === 'personal'/)
  assert.match(
    text,
    /const result = isPersonal\s*\?\s*await skillApi\.updatePersonalSkillEnabled\(skill\.slug, enabled\)\s*:\s*await skillApi\.updateSkillEnabled\(skill\.slug, enabled\)/
  )
  assert.match(text, /\(item\.source_scope === 'personal'\) === isPersonal/)
})

test('套件内已安装技能：左侧图标按钮切换启用，点击技能信息看详情', () => {
  const modal = source('components/extensions/SkillInstallFlowModal.vue')
  // 已安装项不再整行切换：左侧图标按钮负责启停，右侧技能信息负责打开详情
  assert.doesNotMatch(modal, /class="selection-item installed installed-toggle"/)
  assert.match(modal, /class="installed-state-toggle"/)
  assert.match(modal, /@click="toggleSkillEnabled\(skill\)"/)
  assert.match(modal, /class="installed-detail-trigger"/)
  assert.match(modal, /@click="openInstalledSkillDetail\(skill\)"/)
  // 打开详情时必须叠加本地启停覆盖值：installed 是弹窗打开时的快照，
  // 直接用它会让刚停用的技能在详情里仍是「已启用」，还能点「立即使用」跳走
  assert.match(modal, /emit\('preview-skill', \{ \.\.\.installed, enabled: !isSkillDisabled\(skill\) \}\)/)
  assert.doesNotMatch(modal, /emit\('preview-skill', installed\)/)
  assert.doesNotMatch(modal, /:disabled="isInstalled\(skill\.slug\)"/)
  // 未安装项仍保留勾选安装入口
  assert.match(modal, /<label v-if="!isInstalled\(skill\.slug\)" class="selection-item">/)
  // 个人与共享 Skill 分流到不同接口
  assert.match(modal, /await skillApi\.updatePersonalSkillEnabled\(installed\.slug, enabled\)/)
  assert.match(modal, /await skillApi\.updateSkillEnabled\(installed\.slug, enabled\)/)
  // 失败必须可见，不能只写入流程错误
  assert.match(modal, /message\.error\(error\?\.response\?\.data\?\.detail/)
  assert.match(modal, /message\.warning\('未找到该 Skill 的安装记录/)
  assert.match(modal, /message\.warning\('未找到该 Skill 的安装记录，请刷新列表后重试'\)/)
  // 只允许切换过程中禁用，权限与缺记录都要给出提示而不是变成死按钮
  assert.match(modal, /:disabled="isSkillToggling\(skill\)"/)
  // 目标状态必须由覆盖值参与后的有效状态推导；用弹窗打开时的快照会导致连续点击提交同一个值
  assert.match(modal, /const enabled = isSkillDisabled\(skill\)/)
  assert.doesNotMatch(modal, /const enabled = installed\.enabled === false/)
  // 启停提示统一为「Skill 已启用/已禁用」，不拼接 Skill 名称（与广场卡片一致）
  assert.match(modal, /message\.success\(`Skill 已\$\{nextEnabled \? '启用' : '禁用'\}`\)/)
  assert.doesNotMatch(modal, /message\.success\(`已\$\{nextEnabled/)

  const list = source('components/extensions/SkillCardList.vue')
  assert.match(list, /@skills-changed="handleSkillsChanged"/)
  // 套件内启停既要刷新技能列表，也要刷新对话页 @技能 候选
  assert.match(
    list,
    /const handleSkillsChanged = \(\) => \{\s*void fetchSkills\(\)[\s\S]*?refreshSelectedAgentSkillOptions\(agentStore\)\s*\}/
  )
  // 套件内点技能信息复用技能详情预览
  assert.match(list, /@preview-skill="handleSuiteSkillPreview"/)
  assert.match(list, /void openSkillPreview\(skill\)/)
})

test('推荐套件来源全部落在远程安装白名单内', () => {
  const text = source('components/extensions/SkillCardList.vue')
  const start = text.indexOf('const RECOMMENDED_SUITES = [')
  const block = text.slice(start, text.indexOf('\nconst router = useRouter()', start))
  const hosts = [...block.matchAll(/source:\s*'([^']+)'/g)].map((m) => new URL(m[1]).hostname)
  assert.ok(hosts.length > 0, '未解析到任何套件来源')
  // 白名单默认值见 backend/package/yuxi/config/options.py 的 remote_skill_source_policy
  for (const host of hosts) {
    assert.ok(['github.com', 'modelscope.cn'].includes(host), `套件来源不在白名单内: ${host}`)
  }
  // 去 AI 味技能必须已进入广场
  assert.match(block, /slug: 'humanizer-zh'/)
})

test('技能广场下载的个人技能归入技能广场技能，个人上传技能只含自行上传', () => {
  const text = source('components/extensions/SkillCardList.vue')
  const predicate = text.slice(
    text.indexOf('const isPlazaSkill ='),
    text.indexOf('const filteredInstalledSkills =')
  )
  const { isPlazaSkill, isPersonalUploadSkill } = new Function(
    `${predicate}; return { isPlazaSkill, isPersonalUploadSkill }`
  )()
  const plazaDownloaded = { slug: 'docx-manipulation', sourceScope: 'personal', origin: 'remote' }
  const selfUploaded = { slug: 'my-skill', sourceScope: 'personal', origin: 'upload' }
  const shared = { slug: 'pptx', sourceScope: 'shared' }
  const legacyPersonal = { slug: 'legacy', sourceScope: 'personal' }

  assert.equal(isPlazaSkill(plazaDownloaded), true)
  assert.equal(isPlazaSkill(shared), true)
  assert.equal(isPlazaSkill(selfUploaded), false)
  assert.equal(isPersonalUploadSkill(selfUploaded), true)
  assert.equal(isPersonalUploadSkill(plazaDownloaded), false)
  assert.equal(isPersonalUploadSkill(shared), false)
  // 缺少 origin 的历史个人技能按自行上传归类，避免已装技能从「个人上传技能」里消失
  assert.equal(isPlazaSkill(legacyPersonal), false)
  assert.equal(isPersonalUploadSkill(legacyPersonal), true)
  // 两个分类互斥且覆盖全部技能
  for (const skill of [plazaDownloaded, selfUploaded, shared, legacyPersonal]) {
    assert.equal(isPlazaSkill(skill) !== isPersonalUploadSkill(skill), true)
  }
})

test('无 OA 链接来源保留片段和完整文件入口', () => {
  const text = source('components/sources/KbResultGroupedList.vue')
  assert.match(text, /<button\s+v-else[\s\S]*?@click="openFileChunksModal\(fileGroup\)"/)
  assert.match(text, /@click="openFileDetail\(fileGroup\)"/)
  assert.match(text, /<KbFileChunksModal\s+v-model:open="chunksModalVisible"/)
  assert.match(text, /<FileDetailModal\s+v-model:open="fileDetailOpen"/)
})

test('技能广场分类对齐《技能广场分类及分类规则》', () => {
  const text = source('components/extensions/SkillCardList.vue')
  const categories = new Function(
    `${text.slice(text.indexOf('const SKILL_CATEGORIES ='), text.indexOf('const skillAreaTabs ='))}
    return SKILL_CATEGORIES`
  )()
  const suites = new Function(
    `${text.slice(text.indexOf('const RECOMMENDED_SUITES ='), text.indexOf('const router = useRouter()'))}
    return RECOMMENDED_SUITES`
  )()
  const categorizeSkill = new Function(
    `${text.slice(text.indexOf('const SKILL_CATEGORY_BY_NAME ='), text.indexOf('const skillCategory ='))}
    return categorizeSkill`
  )()

  // 只展示有内容的分类页签；数量为 0 的「效率工具」「商业运营」不生成页签
  assert.deepEqual(
    categories.map((item) => item.label),
    ['全部', '办公协同', '知识与学习', '内容创作', '数据分析', '开发工具', '信息资讯']
  )

  // 套件按规则文档归类
  assert.deepEqual(Object.fromEntries(suites.map((suite) => [suite.name, suite.category])), {
    'MiniMax 办公文档套件': 'office',
    'Skill 能力与进化套件': 'devtools',
    'Anthropic 官方文档套件': 'office',
    '演示文稿与视觉套件': 'creation',
    '翻译与写作套件': 'creation',
    '中文去 AI 味套件': 'devtools',
    '研究与竞品分析套件': 'learning',
    '办公自动化套件': 'office'
  })

  // 规则文档点名的内置 Skill 按 slug 归位
  assert.equal(categorizeSkill({ slug: 'image-gen', name: 'image-gen' }), 'creation')
  assert.equal(categorizeSkill({ slug: 'mysql-reporter', name: 'mysql reporter' }), 'analytics')
  assert.equal(categorizeSkill({ slug: 'html-preview', name: 'html-preview' }), 'devtools')
  assert.equal(categorizeSkill({ slug: 'knowledge-base', name: 'knowledge-base' }), 'devtools')
  assert.equal(categorizeSkill({ slug: 'deep-research', name: 'deep-research' }), 'news')
  // 规则点名但项目不存在的 Skill（Huashu Excel）不建占位条目
  assert.equal(
    suites.some(
      (suite) =>
        /huashu/i.test(suite.name) ||
        (suite.skills || []).some((skill) => /huashu/i.test(`${skill.slug || ''}${skill.name || ''}`))
    ),
    false
  )

  // 当前 13 张广场卡片（8 套件 + 5 内置）全部落到已定义分类里，且总数守恒
  const builtinSlugs = ['image-gen', 'mysql-reporter', 'html-preview', 'knowledge-base', 'deep-research']
  const mapped = [...suites.map((suite) => suite.category), ...builtinSlugs.map((slug) => categorizeSkill({ slug, name: slug }))]
  assert.equal(mapped.length, 13)
  assert.equal(
    mapped.every((key) => categories.some((item) => item.key === key)),
    true
  )
  // 未点名技能的兜底也必须落在已存在的分类里，不能出现已删除分类（效率工具/商业运营）
  assert.equal(categorizeSkill({ slug: 'unknown-thing', name: '未知技能' }), 'office')
  // 按现存分类统计：办公协同 3 / 知识与学习 1 / 内容创作 3 / 数据分析 1 / 开发工具 4 / 信息资讯 1
  const countOf = (key) => mapped.filter((item) => item === key).length
  assert.deepEqual(
    ['office', 'learning', 'creation', 'analytics', 'devtools', 'news'].map(countOf),
    [3, 1, 3, 1, 4, 1]
  )
})
