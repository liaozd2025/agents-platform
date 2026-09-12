import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed } from 'vue'

const source = (path) => readFileSync(new URL(`../../src/${path}`, import.meta.url), 'utf8')

test('技能广场同时应用搜索和分类，不匹配时显示空列表', () => {
  const text = source('components/extensions/SkillCardList.vue')
  const expression = text.slice(text.indexOf('const plazaGroups ='), text.indexOf('const mineSkills ='))
  const evaluate = new Function('computed', 'SKILL_CATEGORIES', 'recommendedSuiteCards', 'matchesSearch', 'skillCategory', `${expression}; return plazaGroups.value`)
  const categories = [{ key: 'all', label: '全部' }, { key: 'creation', label: '创作' }]
  const cards = { value: [{ name: 'PDF', category: 'creation' }, { name: 'Search', category: 'research' }] }
  assert.deepEqual(evaluate(computed, categories, cards, () => false, (s) => s.category), [])
  const groups = evaluate(computed, categories, cards, (s) => s.name === 'PDF', (s) => s.category)
  assert.deepEqual(groups.map((g) => g.skills.map((s) => s.name)), [['PDF'], ['PDF']])
})

test('无 OA 链接来源保留片段和完整文件入口', () => {
  const text = source('components/sources/KbResultGroupedList.vue')
  assert.match(text, /<button\s+v-else[\s\S]*?@click="openFileChunksModal\(fileGroup\)"/)
  assert.match(text, /@click="openFileDetail\(fileGroup\)"/)
  assert.match(text, /<KbFileChunksModal\s+v-model:open="chunksModalVisible"/)
  assert.match(text, /<FileDetailModal\s+v-model:open="fileDetailOpen"/)
})
