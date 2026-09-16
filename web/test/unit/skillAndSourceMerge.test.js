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
  // plazaGroups 同时消费广场套件卡与系统技能卡，两者都要注入。
  const evaluate = new Function(
    'computed',
    'SKILL_CATEGORIES',
    'recommendedSuiteCards',
    'installedSkillCards',
    'isSystemSkill',
    'matchesSearch',
    'skillCategory',
    `${expression}; return plazaGroups.value`
  )
  const categories = [{ key: 'all', label: '全部' }, { key: 'creation', label: '创作' }]
  const cards = { value: [{ name: 'PDF', category: 'creation' }, { name: 'Search', category: 'research' }] }
  const noInstalledSkills = { value: [] }
  const run = (matchesSearch, isSystemSkill) =>
    evaluate(
      computed,
      categories,
      cards,
      noInstalledSkills,
      isSystemSkill,
      matchesSearch,
      (s) => s.category
    )
  assert.deepEqual(run(() => false, () => false), [])
  const groups = run((s) => s.name === 'PDF', () => false)
  assert.deepEqual(groups.map((g) => g.skills.map((s) => s.name)), [['PDF'], ['PDF']])
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

test('套件内已安装技能整行可切换启用状态', () => {
  const modal = source('components/extensions/SkillInstallFlowModal.vue')
  // 已安装项不再是禁用的复选框行，而是整行可点的切换入口
  assert.match(modal, /class="selection-item installed installed-toggle"/)
  assert.match(modal, /@click="toggleSkillEnabled\(skill\)"/)
  assert.doesNotMatch(modal, /:disabled="isInstalled\(skill\.slug\)"/)
  // 未安装项仍保留勾选安装入口
  assert.match(modal, /<label v-if="!isInstalled\(skill\.slug\)" class="selection-item">/)
  // 个人与共享 Skill 分流到不同接口
  assert.match(modal, /await skillApi\.updatePersonalSkillEnabled\(installed\.slug, enabled\)/)
  assert.match(modal, /await skillApi\.updateSkillEnabled\(installed\.slug, enabled\)/)
  // 失败必须可见，不能只写入流程错误
  assert.match(modal, /message\.error\(error\?\.response\?\.data\?\.detail/)
  assert.match(modal, /message\.warning\('未找到该 Skill 的安装记录/)
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
  assert.match(list, /const handleSkillsChanged = \(\) => \{\s*void fetchSkills\(\)\s*\}/)
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

test('无 OA 链接来源保留片段和完整文件入口', () => {
  const text = source('components/sources/KbResultGroupedList.vue')
  assert.match(text, /<button\s+v-else[\s\S]*?@click="openFileChunksModal\(fileGroup\)"/)
  assert.match(text, /@click="openFileDetail\(fileGroup\)"/)
  assert.match(text, /<KbFileChunksModal\s+v-model:open="chunksModalVisible"/)
  assert.match(text, /<FileDetailModal\s+v-model:open="fileDetailOpen"/)
})
