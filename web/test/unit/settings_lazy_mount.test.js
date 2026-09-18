import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { compileScript, parse } from 'vue/compiler-sfc'
import { createRenderer, h, nextTick, ref, ssrContextKey } from 'vue'
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'
import { createRouter, createMemoryHistory } from 'vue-router'

test('设置路由仅挂载访问页，切换保留表单，离开重进只挂载当前页', async () => {
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom',
    plugins: [
      {
        name: 'settings-client-template',
        resolveId(id) {
          if (id === 'virtual:settings-test') return '\0' + id
        },
        load(id) {
          if (id.endsWith('/src/utils/asyncPanel.js')) {
            return `import { h, ref } from 'vue'
              export const createAsyncPanel = (loader) => ({ setup() {
                const name = loader.toString().match(/components\\/(\\w+)\\.vue/)[1]
                const draft = ref('')
                return () => h('input', { panel: name, value: draft.value,
                  onInput: (event) => { draft.value = event.target.value } })
              } })`
          }
          if (id !== '\0virtual:settings-test') return
          // 在内存中编译真实客户端模板，避免 SSR 模板跳过 v-show 等交互语义。
          const source = readFileSync(
            new URL('../../src/components/SettingsModal.vue', import.meta.url),
            'utf8'
          )
          const { descriptor } = parse(source)
          return compileScript(descriptor, { id: 'settings-test', inlineTemplate: true }).content
        }
      }
    ]
  })
  const pinia = createPinia()
  setActivePinia(pinia)
  let app
  try {
    const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
    useUserStore().token = 'fixture'
    useUserStore().effectivePermissions = ['system_config:manage', 'ocr:manage']
    const { default: Settings } = await server.ssrLoadModule('virtual:settings-test')
    const makeNode = (type) => ({ type, children: [], props: {}, style: {}, parent: null })
    const renderer = createRenderer({
      createElement: makeNode,
      createText: (text) => ({ ...makeNode('text'), text }),
      createComment: (text) => ({ ...makeNode('comment'), text }),
      insert(child, parent, anchor = null) {
        child.parent = parent
        const index = anchor ? parent.children.indexOf(anchor) : -1
        if (index < 0) parent.children.push(child)
        else parent.children.splice(index, 0, child)
      },
      remove(child) {
        child.parent.children.splice(child.parent.children.indexOf(child), 1)
      },
      setText(node, text) {
        node.text = text
      },
      setElementText(node, text) {
        node.text = text
        node.children = []
      },
      parentNode: (node) => node.parent,
      nextSibling: (node) => node.parent?.children[node.parent.children.indexOf(node) + 1] || null,
      patchProp(node, key, _old, value) {
        node.props[key] = value
      }
    })
    const find = (node, predicate) =>
      predicate(node) ? node : node.children.map((child) => find(child, predicate)).find(Boolean)
    const host = makeNode('root')
    const visible = ref(true)
    const router = createRouter({ history: createMemoryHistory(), routes: [
      ...['ocr', 'account', 'apiKeys', 'agentEnv'].map((tab) => ({ path: '/' + tab, meta: { settingsTab: tab }, component: {} }))
    ] })
    await router.push('/ocr')
    app = renderer.createApp(() => visible.value ? h(Settings) : null)
    app.use(router)
    app.use(pinia)
    app.provide(ssrContextKey, { modules: new Set() })
    app.component('a-modal', {
      inheritAttrs: false,
      props: ['open'],
      emits: ['cancel', 'update:open'],
      setup:
        (props, { slots }) =>
        () =>
          props.open ? slots.default?.() : null
    })
    app.mount(host)
    assert.ok(find(host, (node) => node.props.panel === 'OCRSettingsSection'))
    assert.equal(
      find(host, (node) => node.props.panel === 'AccountSettingsComponent'),
      undefined
    )
    assert.equal(
      find(host, (node) => node.props.panel === 'BasicSettingsSection'),
      undefined
    )
    assert.equal(
      find(host, (node) => node.props.panel === 'UserManagementComponent'),
      undefined
    )
    const ocr = find(host, (node) => node.props.panel === 'OCRSettingsSection')
    ocr.props.onInput({ target: { value: 'draft' } })
    const select = async (label) => {
      const tabs = { '账户设置': 'account', 'OCR 配置': 'ocr', 'API Keys': 'apiKeys', '环境变量': 'agentEnv' }
      await router.push('/' + tabs[label])
      await nextTick()
    }
    await select('账户设置')
    assert.ok(find(host, (node) => node.props.panel === 'AccountSettingsComponent'))
    await select('OCR 配置')
    assert.equal(
      find(host, (node) => node.props.panel === 'OCRSettingsSection'),
      ocr
    )
    assert.equal(ocr.props.value, 'draft')
    await select('API Keys')
    const apiKeys = find(host, (node) => node.props.panel === 'ApiKeyManagementComponent')
    apiKeys.props.onInput({ target: { value: 'api-draft' } })
    await select('账户设置')
    await select('API Keys')
    assert.equal(
      find(host, (node) => node.props.panel === 'ApiKeyManagementComponent'),
      apiKeys
    )
    assert.equal(apiKeys.props.value, 'api-draft')

    await select('环境变量')
    const agentEnv = find(host, (node) => node.props.panel === 'AgentEnvSettingsCard')
    agentEnv.props.onInput({ target: { value: 'env-draft' } })
    await select('账户设置')
    await select('环境变量')
    assert.equal(
      find(host, (node) => node.props.panel === 'AgentEnvSettingsCard'),
      agentEnv
    )
    assert.equal(agentEnv.props.value, 'env-draft')

    visible.value = false
    await nextTick()
    await router.push('/ocr')
    visible.value = true
    await nextTick()
    assert.ok(find(host, (node) => node.props.panel === 'OCRSettingsSection'))
    assert.equal(
      find(host, (node) => node.props.panel === 'AccountSettingsComponent'),
      undefined
    )
  } finally {
    app?.unmount()
    disposePinia(pinia)
    await server.close()
  }
})
