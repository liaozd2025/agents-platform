import { createMemoryHistory, createRouter, routerKey } from 'vue-router'
import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { setImmediate } from 'node:timers'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { renderToString } from 'vue/server-renderer'
import { createTestServer } from '../helpers/createTestServer.js'
import {
  getCooperationPendingActions,
  getSessionAttentionLabel
} from '../../src/modules/session/model/cooperationAttention.js'

let server, CooperationAttention
before(async () => {
  server = await createTestServer({ server: { middlewareMode: true, hmr: false } })
  ;({ default: CooperationAttention } = await server.ssrLoadModule(
    '/src/modules/session/ui/CooperationAttention.vue'
  ))
})
after(async () => {
  await server?.close()
})

const member = (sessionId, waitingFor, status = 'waiting') => ({
  session_id: sessionId,
  name: sessionId,
  turn_status: status,
  waiting_for: waitingFor
})
const render = async (actions, error = '') => {
  const app = createSSRApp(CooperationAttention, { actions, error })
  app.component('a-popover', {
    props: ['trigger', 'placement', 'open', 'afterVisibleChange'],
    emits: ['openChange'],
    setup:
      (_, { slots }) =>
      () =>
        slots.default()
  })
  return renderToString(app)
}

test('人工待办只来自其他成员当前等待中的回答或审批', () => {
  const sessions = [
    member('root', 'approval'),
    member('child', 'answer'),
    member('grandchild', 'approval'),
    member('cooperation', 'cooperation'),
    member('unknown', 'unknown'),
    ...['completed', 'cancelled', 'failed', 'running', 'cancelling'].map((status) =>
      member(status, 'approval', status)
    )
  ]
  assert.deepEqual(getCooperationPendingActions(sessions, 'root'), [
    { sessionId: 'child', name: 'child', label: '待回答', action: '去回答' },
    { sessionId: 'grandchild', name: 'grandchild', label: '待审批', action: '查看审批' }
  ])
  assert.equal(getSessionAttentionLabel(null), '')
  assert.equal(getSessionAttentionLabel(member('done', 'answer', 'completed')), '')
})

test('单项和多项提供明确动作，空列表移除胶囊，查询失败仍显示重试', async () => {
  const actions = getCooperationPendingActions([member('资料整理', 'approval')], 'root')
  const single = await render(actions)
  assert.match(single, /资料整理 待审批/)
  assert.match(single, /查看审批/)
  assert.match(single, /role="status"/)
  const multi = await render([
    ...actions,
    ...getCooperationPendingActions([member('检索', 'answer')], 'root')
  ])
  assert.match(multi, /2 个协作任务需要你处理 · 1 待审批 \/ 1 待回答/)
  assert.match(multi, /查看待办/)
  assert.doesNotMatch(await render([]), /class="attention-pill"/)
  const stale = await render(actions, 'offline')
  assert.match(stale, /资料整理 待审批/)
  assert.match(stale, /协作状态更新失败，显示最近已知待办/)
  assert.match(stale, /重试/)
  assert.match(await render([], 'offline'), /协作状态更新失败/)
})

test('查看单项或选择列表成员只发起导航，不消费待办', async () => {
  const actions = getCooperationPendingActions([member('child', 'answer')], 'root')
  const opened = []
  let state
  await renderToString(
    createSSRApp({
      setup() {
        state = CooperationAttention.setup(
          { actions, error: '' },
          {
            expose() {},
            emit: (event, id) => opened.push([event, id])
          }
        )
        return () => h('div')
      }
    })
  )
  state.handleClick()
  assert.deepEqual(opened, [['open', 'child']])
  assert.equal(actions.length, 1)
  actions.push({ sessionId: 'second', name: 'second', label: '待审批', action: '查看审批' })
  state.handleClick()
  assert.equal(opened.length, 1, '多项不能默认替用户选择成员')
  state.openAction(actions[1])
  assert.deepEqual(opened.at(-1), ['open', 'second'])
  assert.equal(actions.length, 2)
})

test('慢等待点恢复后才聚焦，隐藏或轮次变化撤销聚焦', async (t) => {
  const previousStorage = globalThis.localStorage, previousDocument = globalThis.document
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  const pinia = createPinia()
  setActivePinia(pinia)
  const { agentApi, knowledgeBaseApi, toolApi, threadApi } = await server.ssrLoadModule('/src/apis/index.js')
  let ready, resumed = 0, focused = false
  t.mock.method(agentApi, 'getAgents', () => new Promise((resolve) => { ready = resolve }))
  t.mock.method(knowledgeBaseApi, 'getAccessibleKnowledgeBases', async () => ({ knowledge_bases: [] }))
  t.mock.method(toolApi, 'getTools', async () => ({ data: [] }))
  t.mock.method(agentApi, 'getAgentDetail', async () => { resumed++; return { agent: { agent_id: 'focus-agent', can_run: true } } })
  t.mock.method(threadApi, 'getHistoryThreads', async () => ({ data: [], has_more: false }))
  t.mock.method(threadApi, 'markThreadViewed', async () => ({}))
  const { useUserStore } = await server.ssrLoadModule('/src/modules/identity/model/user.js')
  useUserStore(pinia).effectivePermissions = ['agent:use', 'knowledge_base:read', 'tool:manage']
  const { useSessionRuntimeStore } = await server.ssrLoadModule('/src/modules/session/model/sessionRuntime.js')
  const { default: Workspace } = await server.ssrLoadModule('/src/modules/session/ui/SessionWorkspace.vue')
  globalThis.document = { addEventListener() {}, removeEventListener() {}, visibilityState: 'visible' }
  const renderer = createRenderer({
    createElement: () => ({ children: [] }), createText: () => ({}), createComment: () => ({}),
    insert: (child, parent) => parent.children.push(child), remove() {}, setText() {}, setElementText() {},
    parentNode: () => null, nextSibling: () => null, patchProp() {}
  })
  const props = reactive({ isNewSession: true, visible: true, agentId: 'focus-agent' })
  let state, exposed
  const app = renderer.createApp({ setup() {
    state = Workspace.setup(props, { expose(value) { exposed = value }, emit() {} })
    return () => null
  } })
  app.use(pinia)
  app.provide(routerKey, createRouter({ history: createMemoryHistory('/'), routes: [] }))
  app.provide(ssrContextKey, {})
  app.mount({ children: [] })
  t.after(() => {
    app.unmount()
    disposePinia(pinia)
    if (previousStorage === undefined) delete globalThis.localStorage; else globalThis.localStorage = previousStorage
    if (previousDocument === undefined) delete globalThis.document; else globalThis.document = previousDocument
  })
  await new Promise((resolve) => setImmediate(resolve))
  state.currentThreadId.value = 'thread-focus'
  state.messageInputStageRef.value = { querySelector: () => ({ focus() { focused = true } }) }
  const thread = useSessionRuntimeStore(pinia).getThreadState('thread-focus')
  thread.turnStatus = 'requires_action'
  thread.currentTurnId = 'turn'
  await nextTick()
  const pending = exposed.focusPendingAction()
  await nextTick()
  assert.equal(resumed, 0)
  assert.equal(focused, false)
  ready({ agents: [] })
  await pending
  assert.equal(resumed, 1)
  assert.equal(focused, false, '导航恢复返回时，等待点仍可能未到达')
  const showApproval = () => {
    thread.pendingInterrupt = { interruptedRunId: 'run', waitpointId: 'wait' }
    Object.assign(state.approvalState, { showModal: true, threadId: 'thread-focus', interruptedRunId: 'run', waitpointId: 'wait' })
  }
  const hideApproval = () => { state.approvalState.showModal = false }
  showApproval()
  await nextTick()
  await nextTick()
  assert.equal(focused, true)
  focused = false
  hideApproval()
  await exposed.focusPendingAction()
  props.visible = false
  await nextTick()
  showApproval()
  await nextTick()
  assert.equal(focused, false, '晚到的卡片不能抢隐藏视图的焦点')
  props.visible = true
  hideApproval()
  await exposed.focusPendingAction()
  thread.currentTurnId = 'another-turn'
  await nextTick()
  showApproval()
  await nextTick()
  assert.equal(focused, false)
  hideApproval()
  await exposed.focusPendingAction()
  thread.turnStatus = 'in_progress'
  await nextTick()
  thread.turnStatus = 'requires_action'
  showApproval()
  await nextTick()
  await nextTick()
  assert.equal(focused, false, '同一轮次恢复后，新等待点不能复用旧聚焦意图')
})
