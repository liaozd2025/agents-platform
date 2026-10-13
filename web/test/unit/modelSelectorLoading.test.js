import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'

/** 提供可控的慢请求，验证真实组件的可观察状态。 */
function deferred() {
  let resolve
  let reject
  const promise = new Promise((done, fail) => {
    resolve = done
    reject = fail
  })
  return { promise, resolve, reject }
}

const models = { fixture: { models: [{ spec: 'fixture:chat', display_name: '测试模型' }] } }

test('模型弹层的加载、关闭和失败重试', async (t) => {
  const previousStorage = globalThis.localStorage
  globalThis.localStorage = { getItem: () => null, setItem() {} }
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom',
    plugins: [
      {
        name: 'model-selector-requests',
        enforce: 'pre',
        load(id) {
          if (id.endsWith('/src/apis/system_api.js'))
            return 'export const modelProviderApi = globalThis.__modelSelectorApi'
          if (id.endsWith('/src/stores/user.js'))
            return 'export const useUserStore = () => ({ isAdmin: false })'
          if (id.endsWith('/src/utils/modelMetadata.js'))
            return `
          export const loadModelMetadataCatalog = () => globalThis.__modelSelectorCatalog.promise
          export const resolveModelDisplayMetadata = () => ({ matched: false })`
        }
      }
    ]
  })
  try {
    let request
    let requestedType
    globalThis.__modelSelectorApi = {
      getV2Models: (type) => {
        requestedType = type
        return request.promise
      },
      refreshModelCache: async () => ({ success: true })
    }
    const { default: ModelSelector } = await server.ssrLoadModule(
      '/src/components/ModelSelectorComponent.vue'
    )
    const setup = async (overrides = {}) => {
      let component
      await renderToString(
        createSSRApp({
          setup() {
            component = ModelSelector.setup(
              { disabled: false, size: 'nano', model_spec: '', modelType: 'chat', ...overrides },
              { expose() {}, emit() {} }
            )
            return () => h('div')
          }
        })
      )
      return component
    }

    await t.test(
      '未完成请求时立即展开，关闭后完成请求也不重新打开',
      { timeout: 2000 },
      async () => {
        request = deferred()
        globalThis.__modelSelectorCatalog = deferred()
        const component = await setup()
        component.handleOpenChange(true)
        assert.equal(component.dropdownOpen.value, true)
        assert.equal(component.loadingV2Models.value, true)
        component.handleOpenChange(false)
        request.resolve({ success: true, data: models })
        await component.fetchV2Models()
        assert.equal(component.dropdownOpen.value, false)
        assert.equal(component.loadingV2Models.value, false)
        assert.deepEqual(component.v2Models.value, models)
      }
    )

    await t.test('目录先显示，不等待可选元数据', { timeout: 2000 }, async () => {
      request = deferred()
      globalThis.__modelSelectorCatalog = deferred()
      const component = await setup()
      component.handleOpenChange(true)
      request.resolve({ success: true, data: models })
      await component.fetchV2Models()
      assert.equal(component.loadingV2Models.value, false)
      assert.equal(component.hasFilteredModels.value, true)
      assert.equal(component.dropdownOpen.value, true)
    })

    await t.test('请求失败保留展开并提供错误，重试成功清除错误', { timeout: 2000 }, async () => {
      request = deferred()
      globalThis.__modelSelectorCatalog = deferred()
      const component = await setup()
      component.handleOpenChange(true)
      request.reject(new Error('fixture unavailable'))
      await component.fetchV2Models()
      assert.equal(component.dropdownOpen.value, true)
      assert.equal(component.loadingV2Models.value, false)
      assert.ok(component.modelsError.value)
      request = deferred()
      const retry = component.fetchV2Models()
      request.resolve({ success: true, data: models })
      await retry
      assert.equal(component.modelsError.value, '')
      assert.deepEqual(component.v2Models.value, models)
    })
    await t.test('Embedding 和 Re-ranker 只展示各自目录，禁用时不打开', async () => {
      for (const modelType of ['embedding', 'rerank']) {
        request = deferred()
        const component = await setup({ modelType })
        component.handleOpenChange(true)
        request.resolve({ success: true, data: models })
        await component.fetchV2Models()
        assert.equal(requestedType, modelType)
        assert.deepEqual(component.v2Models.value, models)
        const disabled = await setup({ modelType, disabled: true })
        disabled.handleOpenChange(true)
        assert.equal(disabled.dropdownOpen.value, false)
        assert.equal(disabled.loadingV2Models.value, false)
      }
    })
    await t.test('异步会话模型、跨会话及再次打开同一会话均恢复，重复目录不重复通知', async () => {
      const props = reactive({
        disabled: false,
        size: 'nano',
        model_spec: 'fixture:chat',
        modelType: 'chat',
        selectionScope: 'A',
        fallbackWhenUnavailable: true
      })
      const events = []
      let component
      const renderer = createRenderer({
        createElement: () => ({}),
        createText: () => ({}),
        createComment: () => ({}),
        insert() {},
        remove() {},
        setText() {},
        setElementText() {},
        patchProp() {},
        parentNode: () => null,
        nextSibling: () => null
      })
      request = deferred()
      globalThis.__modelSelectorCatalog = deferred()
      const app = renderer.createApp({
        setup() {
          component = ModelSelector.setup(props, {
            expose() {},
            emit: (...event) => events.push(event)
          })
          return () => h('div')
        }
      })
      app.provide(ssrContextKey, { modules: new Set() })
      app.mount({})
      try {
        request.resolve({ success: true, data: models })
        await component.fetchV2Models()
        assert.deepEqual(events, [])
        props.model_spec = 'fixture:removed'
        await nextTick()
        assert.deepEqual(events, [
          [
            'select-model',
            'fixture:chat',
            { autoSelected: true, unavailableSpec: 'fixture:removed' }
          ]
        ])
        request = deferred()
        const refreshed = component.fetchV2Models()
        request.resolve({ success: true, data: models })
        await refreshed
        assert.equal(events.length, 1)
        props.selectionScope = 'B'
        await nextTick()
        assert.equal(events.length, 2)
        props.selectionScope = 'A'
        await nextTick()
        assert.equal(events.length, 3)
        props.model_spec = 'fixture:chat'
        await nextTick()
        props.model_spec = 'fixture:removed'
        await nextTick()
        assert.equal(events.length, 4)
      } finally {
        app.unmount()
      }
    })
    await t.test('表单包装器保留 update:value 与 change 的选择结果', async () => {
      for (const name of ['EmbeddingModelSelector', 'RerankModelSelector']) {
        const { default: Wrapper } = await server.ssrLoadModule(`/src/components/${name}.vue`)
        const events = []
        let component
        await renderToString(
          createSSRApp({
            setup() {
              component = Wrapper.setup({}, { expose() {}, emit: (...event) => events.push(event) })
              return () => h('div')
            }
          })
        )
        component.handleSelect('fixture:selected')
        assert.deepEqual(events, [
          ['update:value', 'fixture:selected'],
          ['change', 'fixture:selected']
        ])
      }
    })
  } finally {
    await server.close()
    globalThis.localStorage = previousStorage
    delete globalThis.__modelSelectorApi
    delete globalThis.__modelSelectorCatalog
  }
})
