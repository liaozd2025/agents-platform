import assert from 'node:assert/strict'
import test from 'node:test'
import { ESLint } from 'eslint'
import { createSSRApp, h, ref } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { routerKey, matchedRouteKey } from 'vue-router'
import Antd from 'ant-design-vue'
import { createTestServer } from '../helpers/createTestServer.js'

test('模板 gate 拒绝未定义属性和遗漏的组件导入', async () => {
  const eslint = new ESLint()
  const [result] = await eslint.lintText(`<script setup>
const agents = [{ id: 'test' }]
</script>
<template><div><MissingIcon /><div v-for="agent in agents" :key="agent.id" v-if="group.key">{{ missingCount }}</div></div></template>`, {
    filePath: 'src/modules/agents/ui/RuntimeRegression.vue'
  })
  assert.ok(result.messages.some((item) => item.ruleId === 'vue/no-undef-components' && item.message.includes('MissingIcon')))
  assert.ok(result.messages.some((item) => item.ruleId === 'vue/no-undef-properties' && item.message.includes('group')))
  assert.ok(result.messages.some((item) => item.ruleId === 'vue/no-undef-properties' && item.message.includes('missingCount')))
})

test('真实智能体卡片模板呈现列表、去对话入口和无权限状态', async (t) => {
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  globalThis.document = { compatMode: 'CSS1Compat', documentElement: { classList: { add() {}, remove() {} } } }
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' })
  const server = await createTestServer({ server: { middlewareMode: true } })
  const pinia = createPinia()
  setActivePinia(pinia)
  t.after(async () => { disposePinia(pinia); await server.close(); delete globalThis.localStorage; delete globalThis.document; delete globalThis.getComputedStyle })
  const { useUserStore } = await server.ssrLoadModule('/src/modules/identity/model/user.js')
  const user = useUserStore()
  user.effectivePermissions = ['agent:use', 'agent:manage']
  const { agentApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
  const agents = [
    { agent_id: 'readable', name: '可运行助手', can_run: true, can_manage: true },
    { agent_id: 'readonly', name: '只读助手', can_run: false, can_manage: false }
  ]
  t.mock.method(agentApi, 'getAgents', async () => ({ agents }))
  const { default: component } = await server.ssrLoadModule('/src/modules/agents/ui/AgentManagePanel.vue')
  const warnings = []
  const navigations = []
  let state
  const render = async () => {
    const app = createSSRApp({ ...component, async setup(props, context) {
      state = component.setup(props, context)
      await state.refreshAgents()
      return state
    } })
    app.use(pinia).use(Antd)
    app.provide(routerKey, { push: (target) => navigations.push(target) })
    app.provide(matchedRouteKey, ref({ leaveGuards: new Set(), updateGuards: new Set() }))
    app.config.warnHandler = (warning) => warnings.push(warning)
    return renderToString(app)
  }
  const html = await render()
  assert.match(html, /可运行助手/)
  assert.match(html, /只读助手/)
  assert.equal((html.match(/去对话/g) || []).length, 1)
  state.openAgentChat(agents[0])
  state.openAgentChat(agents[1])
  assert.deepEqual(navigations, [{ name: 'AgentComp', query: { agent_id: 'readable' } }])
  user.effectivePermissions = []
  assert.doesNotMatch(await render(), /去对话|新增智能体/)
  assert.deepEqual(warnings, [])
})

test('技能套件真实模板显示安装计数、图标颜色和边框', async () => {
  const server = await createTestServer({ server: { middlewareMode: true } })
  try {
    const { default: component } = await server.ssrLoadModule('/src/modules/extensions/ui/SkillSuiteCard.vue')
    const app = createSSRApp({ render: () => h(component, {
      suite: { id: 'office', name: '办公套件', provider: '测试', skills: [{ slug: 'writer' }, { slug: 'reader' }] },
      installedSlugs: ['writer'], iconColor: '#123456', iconBorder: true
    }) })
    const warnings = []
    app.config.warnHandler = (warning) => warnings.push(warning)
    const html = await renderToString(app)
    assert.match(html, /已安装 1\/2/)
    assert.match(html, /suite-card-icon-colored suite-card-icon-bordered/)
    assert.match(html, /--suite-icon-color:#123456/)
    assert.deepEqual(warnings, [])
  } finally { await server.close() }
})

test('账户真实模板显示企业标识，复制成功和失败均有反馈', async (t) => {
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  globalThis.document = { compatMode: 'CSS1Compat', documentElement: { classList: { add() {}, remove() {} } } }
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' })
  const server = await createTestServer({ server: { middlewareMode: true } })
  const pinia = createPinia()
  setActivePinia(pinia)
  t.after(async () => { disposePinia(pinia); await server.close(); delete globalThis.localStorage; delete globalThis.document; delete globalThis.getComputedStyle })
  const { useUserStore } = await server.ssrLoadModule('/src/modules/identity/model/user.js')
  useUserStore().uid = 'oidc:test-account'
  const { message } = await import('ant-design-vue')
  const successes = [], errors = [], copied = []
  t.mock.method(message, 'success', (value) => successes.push(value))
  t.mock.method(message, 'error', (value) => errors.push(value))
  const { default: component } = await server.ssrLoadModule('/src/modules/settings/ui/AccountSettingsComponent.vue')
  let state
  const app = createSSRApp({ ...component, setup(props, context) {
    state = component.setup(props, context)
    state.identifierVisible.value = true
    return state
  } })
  app.use(pinia).use(Antd)
  app.provide(routerKey, { push() {} })
  app.provide('settingsModal', { closeSettingsModal() {} })
  const warnings = []
  app.config.warnHandler = (warning) => warnings.push(warning)
  const html = await renderToString(app)
  assert.match(html, /账户设置/)
  assert.match(html, /oidc:test-account/)
  assert.match(html, /lucide-chevron-up/)
  assert.match(html, /lucide-copy/)
  const previousNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
  globalThis.window = { isSecureContext: true }
  const clipboard = { writeText: async (value) => copied.push(value) }
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard } })
  t.after(() => {
    delete globalThis.window
    if (previousNavigator) Object.defineProperty(globalThis, 'navigator', previousNavigator)
    else delete globalThis.navigator
  })
  await state.copyIdentifier()
  assert.deepEqual(copied, ['oidc:test-account'])
  assert.deepEqual(successes, ['账号 ID 已复制'])
  clipboard.writeText = async () => { throw new Error('denied') }
  const textarea = { value: '', style: {}, setAttribute() {}, focus() {}, select() {} }
  let fallbackSucceeds = true
  let attached
  let fallbackCalls = 0
  Object.assign(globalThis.document, {
    createElement: (tag) => { assert.equal(tag, 'textarea'); return textarea },
    body: {
      appendChild: (node) => { attached = node },
      removeChild: (node) => { assert.equal(node, attached); attached = null }
    },
    execCommand: (command) => {
      assert.equal(command, 'copy')
      assert.equal(attached.value, 'oidc:test-account')
      fallbackCalls++
      return fallbackSucceeds
    }
  })
  await state.copyIdentifier()
  assert.equal(successes.length, 2)
  assert.equal(attached, null)
  fallbackSucceeds = false
  await state.copyIdentifier()
  assert.equal(fallbackCalls, 2)
  assert.equal(attached, null)
  assert.deepEqual(errors, ['复制失败，请手动复制'])
  assert.deepEqual(warnings, [])
})
