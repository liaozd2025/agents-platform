import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { refreshSelectedAgentSkillOptions } from '../../src/utils/agent_skill_options.js'

const source = (path) => readFileSync(new URL(`../../src/${path}`, import.meta.url), 'utf8')

/** 记录调用的假 agent store。 */
const createFakeAgentStore = ({ selectedAgentId = 1, fail = false } = {}) => {
  const calls = []
  return {
    selectedAgentId,
    calls,
    fetchAgentDetail: async (agentId, forceRefresh) => {
      calls.push([agentId, forceRefresh])
      if (fail) throw new Error('boom')
    }
  }
}

test('未选中 Agent 时不发起刷新', async () => {
  const store = createFakeAgentStore({ selectedAgentId: null })
  assert.equal(await refreshSelectedAgentSkillOptions(store), false)
  assert.deepEqual(store.calls, [])
})

test('强制刷新当前 Agent 详情（绕过缓存）', async () => {
  const store = createFakeAgentStore({ selectedAgentId: 7 })
  assert.equal(await refreshSelectedAgentSkillOptions(store), true)
  // 第二个参数必须是 true，否则会命中 agentDetails 缓存、技能候选不会更新
  assert.deepEqual(store.calls, [[7, true]])
})

test('刷新失败不抛出，避免打断技能启停主流程', async () => {
  const store = createFakeAgentStore({ selectedAgentId: 7, fail: true })
  assert.equal(await refreshSelectedAgentSkillOptions(store), false)
  assert.deepEqual(store.calls, [[7, true]])
})

test('技能启停/卸载的每个入口都会刷新 Agent 技能候选', () => {
  const list = source('components/extensions/SkillCardList.vue')
  // 卡片与预览开关切换
  assert.match(
    list,
    /message\.success\(`Skill 已\$\{enabled \? '启用' : '禁用'\}`\)\s*\/\/[^\n]*\n\s*await refreshSelectedAgentSkillOptions\(agentStore\)/
  )
  // 卸载
  assert.match(
    list,
    /await fetchSkills\(\)\s*\/\/[^\n]*\n\s*await refreshSelectedAgentSkillOptions\(agentStore\)/
  )
  // 套件弹窗内启停
  assert.match(
    list,
    /const handleSkillsChanged = \(\) => \{\s*void fetchSkills\(\)\s*\/\/[^\n]*\n\s*void refreshSelectedAgentSkillOptions\(agentStore\)\s*\}/
  )
  // 安装完成
  assert.match(
    list,
    /if \(success > 0\) \{\s*await refreshSelectedAgentSkillOptions\(agentStore\)\s*\}/
  )
  // 不允许再各写一份刷新逻辑
  assert.doesNotMatch(list, /await agentStore\.fetchAgentDetail\(selectedAgentId, true\)/)

  const detail = source('components/extensions/SkillDetailView.vue')
  assert.match(
    detail,
    /message\.success\('设置已保存'\)\s*\/\/[^\n]*\n\s*await refreshSelectedAgentSkillOptions\(agentStore\)/
  )
  assert.match(
    detail,
    /message\.success\(`已\$\{actionText\}`\)\s*\/\/[^\n]*\n\s*await refreshSelectedAgentSkillOptions\(agentStore\)/
  )
})
