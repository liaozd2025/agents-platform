import assert from 'node:assert/strict'
import path from 'node:path'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
let server
let agentApi
let useAgentRequestQueue
let useAgentRunStream
let useAgentStreamHandler

before(async () => {
  const storage = new Map()
  globalThis.localStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key)
  }
  server = await createServer({ root: webRoot, server: { middlewareMode: true } })
  ;({ agentApi } = await server.ssrLoadModule('/src/apis/index.js'))
  ;({ useAgentRequestQueue } = await server.ssrLoadModule(
    '/src/composables/useAgentRequestQueue.js'
  ))
  ;({ useAgentRunStream } = await server.ssrLoadModule('/src/composables/useAgentRunStream.js'))
  ;({ useAgentStreamHandler } = await server.ssrLoadModule(
    '/src/composables/useAgentStreamHandler.js'
  ))
})

after(async () => {
  await server?.close()
  delete globalThis.localStorage
})

test('排队请求在等待 Run 创建期间保持 loading 状态', async () => {
  const threadState = {
    queuedRequests: [],
    requestStreams: {},
    onGoingConv: { msgChunks: {} },
    pendingRequestId: 'request-1',
    isStreaming: true,
    replyLoadingVisible: true
  }
  const originalStreamRequestEvents = agentApi.streamRequestEvents
  agentApi.streamRequestEvents = async () =>
    new Response('event: queued\ndata: {"position":1}\n\n', {
      headers: { 'Content-Type': 'text/event-stream' }
    })

  try {
    const queue = useAgentRequestQueue({
      getThreadState: () => threadState,
      resetOnGoingConv: () => {},
      startRunStream: () => {},
      onStreamError: () => {}
    })

    await queue.startRequestStream('thread-1', 'request-1')

    assert.equal(threadState.replyLoadingVisible, true)
  } finally {
    agentApi.streamRequestEvents = originalStreamRequestEvents
  }
})

/** 集中 Run SSE 测试的固定依赖，只暴露各用例关心的行为。 */
const createRunStream = ({ threadState, handleStreamChunk, resetOnGoingConv, fetchThreadMessages }) =>
  useAgentRunStream({
    getThreadState: () => threadState,
    currentAgentId: { value: 'agent-1' },
    handleStreamChunk,
    fetchThreadMessages: fetchThreadMessages || (async () => {}),
    fetchAgentState: () => {},
    resetOnGoingConv,
    onScrollToBottom: () => {},
    streamSmoother: { flushThread: () => {} }
  })

test('agent_state SSE 使在途状态请求失效', () => {
  const threadState = {
    agentState: null,
    agentStateRequestVersion: 4,
    onGoingConv: { msgChunks: {} }
  }
  const { handleStreamChunk } = useAgentStreamHandler({
    getThreadState: () => threadState,
    processApprovalInStream: () => false,
    currentAgentId: { value: 'agent-1' },
    supportsFiles: { value: false }
  })
  const agentState = { token_usage: { measured_at: '2026-08-09T00:00:00Z' } }

  handleStreamChunk({ status: 'agent_state', agent_state: agentState }, 'thread-1')

  assert.deepEqual(threadState.agentState, agentState)
  assert.equal(threadState.agentStateRequestVersion, 5)
})

test('run_created 立即完成状态交接并订阅新 Run SSE', async () => {
  const threadState = {
    queuedRequests: [{ request_id: 'request-1', status: 'queued' }],
    requestStreams: {},
    onGoingConv: { msgChunks: { old: [] } },
    pendingRequestId: null
  }
  const calls = []
  const originalStreamRequestEvents = agentApi.streamRequestEvents
  agentApi.streamRequestEvents = async () =>
    new Response('event: run_created\ndata: {"run_id":"run-2"}\n\n', {
      headers: { 'Content-Type': 'text/event-stream' }
    })

  try {
    const queue = useAgentRequestQueue({
      getThreadState: () => threadState,
      resetOnGoingConv: (threadId, options) => {
        calls.push(['reset', threadId, options])
        threadState.onGoingConv = { msgChunks: {} }
      },
      startRunStream: (...args) => {
        calls.push(['start', ...args])
      },
      onStreamError: () => {}
    })

    await queue.startRequestStream('thread-1', 'request-1')

    assert.deepEqual(calls, [
      ['reset', 'thread-1', { preserveRequestStreams: true }],
      ['start', 'thread-1', 'run-2', '0-0']
    ])
    assert.equal(threadState.pendingRequestId, 'request-1')
    assert.deepEqual(threadState.queuedRequests, [])
  } finally {
    agentApi.streamRequestEvents = originalStreamRequestEvents
  }
})

test('run_created 先到达时保留旧 Run 已渲染的内容', async () => {
  const oldMessages = { 'old-message': [{ id: 'old-message', content: '旧回复' }] }
  const threadState = {
    activeRunId: 'run-1',
    queuedRequests: [{ request_id: 'request-2', status: 'queued' }],
    requestStreams: {},
    onGoingConv: { msgChunks: oldMessages },
    pendingRequestId: null
  }
  const calls = []
  const originalStreamRequestEvents = agentApi.streamRequestEvents
  agentApi.streamRequestEvents = async () =>
    new Response('event: run_created\ndata: {"run_id":"run-2"}\n\n', {
      headers: { 'Content-Type': 'text/event-stream' }
    })

  try {
    const queue = useAgentRequestQueue({
      getThreadState: () => threadState,
      resetOnGoingConv: () => calls.push(['reset']),
      startRunStream: (...args) => calls.push(['start', ...args]),
      onStreamError: () => {}
    })

    await queue.startRequestStream('thread-1', 'request-2')

    assert.deepEqual(calls, [['start', 'thread-1', 'run-2', '0-0']])
    assert.equal(threadState.onGoingConv.msgChunks, oldMessages)
    assert.equal(threadState.pendingRequestId, 'request-2')
  } finally {
    agentApi.streamRequestEvents = originalStreamRequestEvents
  }
})

test('replacement Run SSE 的增量 chunk 会连续进入前端渲染处理', async () => {
  const oldMessages = { 'old-message': [{ id: 'old-message', content: '旧回复' }] }
  const threadState = {
    activeRunId: 'run-1',
    activeRunSteerable: true,
    queuedRequests: [{ request_id: 'request-2', status: 'queued' }],
    requestStreams: {},
    runLastSeq: '0-0',
    runStreamAbortController: null,
    replyLoadingVisible: true,
    pendingRequestId: 'request-1',
    pendingInterrupt: null,
    onGoingConv: { msgChunks: oldMessages }
  }
  const originalStreamRequestEvents = agentApi.streamRequestEvents
  const originalStreamAgentRunEvents = agentApi.streamAgentRunEvents
  const textEncoder = new TextEncoder()
  let runStreamController
  let replacementRun
  let loadingChunkCount = 0
  let resolveFirstChunk
  let resolveSecondChunk
  const firstChunkProcessed = new Promise((resolve) => {
    resolveFirstChunk = resolve
  })
  const secondChunkProcessed = new Promise((resolve) => {
    resolveSecondChunk = resolve
  })

  agentApi.streamRequestEvents = async () =>
    new Response('event: run_created\ndata: {"run_id":"run-2"}\n\n', {
      headers: { 'Content-Type': 'text/event-stream' }
    })
  agentApi.streamAgentRunEvents = async () =>
    new Response(
      new ReadableStream({
        start(controller) {
          runStreamController = controller
        }
      }),
      { headers: { 'Content-Type': 'text/event-stream' } }
    )

  try {
    const { handleStreamChunk } = useAgentStreamHandler({
      getThreadState: () => threadState,
      processApprovalInStream: () => false,
      currentAgentId: { value: 'agent-1' },
      supportsFiles: { value: false }
    })
    const runStream = createRunStream({
      threadState,
      handleStreamChunk: (chunk, threadId) => {
        const shouldStop = handleStreamChunk(chunk, threadId)
        if (chunk.status === 'loading') {
          loadingChunkCount += 1
          if (loadingChunkCount === 1) resolveFirstChunk()
          if (loadingChunkCount === 2) resolveSecondChunk()
        }
        return shouldStop
      },
      resetOnGoingConv: () => {}
    })
    const queue = useAgentRequestQueue({
      getThreadState: () => threadState,
      resetOnGoingConv: () => {},
      startRunStream: (...args) => {
        replacementRun = runStream.startRunStream(...args)
        return replacementRun
      },
      onStreamError: () => {}
    })

    await queue.startRequestStream('thread-1', 'request-2')

    runStreamController.enqueue(
      textEncoder.encode(
        'id: 1-0\nevent: custom\ndata: {"run_id":"run-2","request_id":"request-2","payload":{"chunk":{"status":"loading","run_id":"run-2","request_id":"request-2","stream_event":{"type":"message_delta","message_id":"assistant-2","content":"流式"}}}}\n\n'
      )
    )
    await firstChunkProcessed
    assert.deepEqual(
      threadState.onGoingConv.msgChunks['assistant-2'].map((chunk) => chunk.content),
      ['流式']
    )

    runStreamController.enqueue(
      textEncoder.encode(
        'id: 2-0\nevent: custom\ndata: {"run_id":"run-2","request_id":"request-2","payload":{"chunk":{"status":"loading","run_id":"run-2","request_id":"request-2","stream_event":{"type":"message_delta","message_id":"assistant-2","content":"渲染"}}}}\n\n'
      )
    )
    await secondChunkProcessed
    assert.deepEqual(
      threadState.onGoingConv.msgChunks['assistant-2'].map((chunk) => chunk.content),
      ['流式', '渲染']
    )

    runStreamController.enqueue(
      textEncoder.encode(
        'id: 3-0\nevent: end\ndata: {"run_id":"run-2","payload":{"status":"completed"}}\n\n'
      )
    )
    runStreamController.close()
    await replacementRun

    assert.deepEqual(threadState.onGoingConv.msgChunks['old-message'], [
      { id: 'old-message', content: '旧回复' }
    ])
    assert.equal(threadState.runLastSeq, '3-0')
  } finally {
    agentApi.streamRequestEvents = originalStreamRequestEvents
    agentApi.streamAgentRunEvents = originalStreamAgentRunEvents
  }
})

test('旧 Run 的延迟 AbortError 不覆盖新 Run 状态', async () => {
  const threadState = {
    activeRunId: null,
    activeRunSteerable: false,
    runLastSeq: '0-0',
    runStreamAbortController: null,
    replyLoadingVisible: false,
    pendingRequestId: null,
    onGoingConv: { msgChunks: {} }
  }
  const streamControllers = new Map()
  const originalStreamAgentRunEvents = agentApi.streamAgentRunEvents
  agentApi.streamAgentRunEvents = async (runId, _afterSeq, { signal }) =>
    new Response(
      new ReadableStream({
        start(controller) {
          streamControllers.set(runId, controller)
          signal.addEventListener('abort', () => {
            const delay = runId === 'run-1' ? 10 : 0
            setTimeout(() => controller.error(new DOMException('aborted', 'AbortError')), delay)
          })
          if (runId === 'run-2') {
            controller.enqueue(
              new TextEncoder().encode(
                'event: custom\ndata: {"run_id":"run-2","request_id":"request-2","payload":{"chunk":{"status":"init","msg":{"type":"human","content":"new"}}}}\n\n'
              )
            )
          }
        }
      }),
      { headers: { 'Content-Type': 'text/event-stream' } }
    )

  const runStream = createRunStream({
    threadState,
    handleStreamChunk: (chunk) => {
      if (chunk.status !== 'init') return
      threadState.pendingRequestId = chunk.request_id
      threadState.replyLoadingVisible = true
    },
    resetOnGoingConv: () => {}
  })

  try {
    const oldRun = runStream.startRunStream('thread-1', 'run-1')
    await new Promise((resolve) => setTimeout(resolve, 0))
    const newRun = runStream.startRunStream('thread-1', 'run-2')
    await new Promise((resolve) => setTimeout(resolve, 20))

    assert.equal(threadState.activeRunId, 'run-2')
    assert.equal(threadState.pendingRequestId, 'request-2')
    assert.equal(threadState.replyLoadingVisible, true)

    threadState.runStreamAbortController.abort()
    await Promise.allSettled([oldRun, newRun])
  } finally {
    agentApi.streamAgentRunEvents = originalStreamAgentRunEvents
  }
})

test('旧 Run 终态清理保留排队 Request SSE', async () => {
  const threadState = {
    activeRunId: null,
    activeRunSteerable: false,
    runLastSeq: '0-0',
    runStreamAbortController: null,
    replyLoadingVisible: false,
    pendingRequestId: null,
    pendingInterrupt: null,
    requestStreams: { 'request-2': { controller: new AbortController() } }
  }
  const resetCalls = []
  const originalStreamAgentRunEvents = agentApi.streamAgentRunEvents
  agentApi.streamAgentRunEvents = async () =>
    new Response('event: end\ndata: {"run_id":"run-1","payload":{"status":"completed"}}\n\n', {
      headers: { 'Content-Type': 'text/event-stream' }
    })

  try {
    const runStream = createRunStream({
      threadState,
      handleStreamChunk: () => {},
      resetOnGoingConv: (_threadId, options) => resetCalls.push(options)
    })

    await runStream.startRunStream('thread-1', 'run-1')
    await new Promise((resolve) => setTimeout(resolve, 0))

    assert.deepEqual(resetCalls, [{ preserveRequestStreams: true }])
    assert.equal(threadState.requestStreams['request-2'].controller.signal.aborted, false)
  } finally {
    agentApi.streamAgentRunEvents = originalStreamAgentRunEvents
  }
})

test('本地流还开着但 Run 已终态时收敛视图，避免残留实时消息挡住末轮来源', async () => {
  // 复现路径：页面重新可见触发 resume 分支，它抢先发现 run 已终态并清空 activeRunId，
  // 随后到达的 SSE finished 会被 finalizeRunStream 的早退判断跳过（activeRunId !== runId）。
  // 若此处不补一次「刷新历史 + 清实时消息」，本轮实时消息会残留成一条 streaming 尾巴轮：
  // 该轮自身不渲染 refs，其前一历史轮又被判为未收尾，末轮「来源」按钮据此消失。
  const residualChunks = { 'msg-1': [{ id: 'msg-1', type: 'ai', content: '残留回复' }] }
  const threadState = {
    activeRunId: 'run-1',
    activeRunSteerable: false,
    runLastSeq: '1700000000001-0',
    runStreamAbortController: new AbortController(),
    replyLoadingVisible: false,
    pendingRequestId: null,
    pendingInterrupt: null,
    isStreaming: true,
    onGoingConv: { msgChunks: residualChunks }
  }
  const fetchCalls = []
  const resetCalls = []
  const originalGetAgentRun = agentApi.getAgentRun
  agentApi.getAgentRun = async () => ({ run: { id: 'run-1', status: 'completed' } })

  try {
    const runStream = createRunStream({
      threadState,
      handleStreamChunk: () => {},
      resetOnGoingConv: (threadId, options) => {
        resetCalls.push([threadId, options])
        threadState.onGoingConv = { msgChunks: {} }
      },
      fetchThreadMessages: async (payload) => {
        fetchCalls.push(payload)
      }
    })

    await runStream.resumeActiveRunForThread('thread-1')
    // 收敛会延迟 200ms 等后端事务提交，用例需等待其完成后再断言
    await new Promise((resolve) => setTimeout(resolve, 300))

    assert.equal(threadState.activeRunId, null)
    // 只断言本次收敛真正关心的契约：刷新当前线程历史，并留出后端事务提交的延迟
    assert.equal(fetchCalls.length, 1)
    assert.equal(fetchCalls[0].threadId, 'thread-1')
    assert.equal(fetchCalls[0].delay, 200)
    assert.deepEqual(resetCalls, [['thread-1', { preserveRequestStreams: true }]])
    assert.deepEqual(threadState.onGoingConv.msgChunks, {})
  } finally {
    agentApi.getAgentRun = originalGetAgentRun
  }
})

test('无活跃 Run 且仍有实时消息残留时补一次收敛', async () => {
  // 页面重新可见时本地已无 Run 流订阅，但上一轮收尾没收敛、实时消息还在，
  // 这种残留同样会让末轮 refs（含来源）不渲染，需要在权威确认无活跃 run 后补一次。
  const residualChunks = { 'msg-9': [{ id: 'msg-9', type: 'ai', content: '残留回复' }] }
  const threadState = {
    activeRunId: null,
    activeRunSteerable: false,
    runLastSeq: '0-0',
    runStreamAbortController: null,
    replyLoadingVisible: false,
    pendingRequestId: null,
    pendingInterrupt: null,
    isStreaming: false,
    onGoingConv: { msgChunks: residualChunks }
  }
  const fetchCalls = []
  const resetCalls = []
  const originalGetThreadActiveRun = agentApi.getThreadActiveRun
  agentApi.getThreadActiveRun = async () => ({ run: null })

  try {
    const runStream = createRunStream({
      threadState,
      handleStreamChunk: () => {},
      resetOnGoingConv: (threadId, options) => {
        resetCalls.push([threadId, options])
        threadState.onGoingConv = { msgChunks: {} }
      },
      fetchThreadMessages: async (payload) => {
        fetchCalls.push(payload)
      }
    })

    await runStream.resumeActiveRunForThread('thread-2')
    await new Promise((resolve) => setTimeout(resolve, 300))

    assert.equal(fetchCalls.length, 1)
    assert.equal(fetchCalls[0].threadId, 'thread-2')
    assert.deepEqual(resetCalls, [['thread-2', { preserveRequestStreams: true }]])
    assert.deepEqual(threadState.onGoingConv.msgChunks, {})
  } finally {
    agentApi.getThreadActiveRun = originalGetThreadActiveRun
  }
})

test('活跃 Run 查询失败时不清实时消息', async () => {
  // 查询失败可能是网络抖动，此时 run 仍可能在跑：不能据此清场，否则会丢掉尚未落库的实时输出。
  const residualChunks = { 'msg-10': [{ id: 'msg-10', type: 'ai', content: '可能仍在生成' }] }
  const threadState = {
    activeRunId: null,
    activeRunSteerable: false,
    runLastSeq: '0-0',
    runStreamAbortController: null,
    replyLoadingVisible: false,
    pendingRequestId: null,
    pendingInterrupt: null,
    isStreaming: false,
    onGoingConv: { msgChunks: residualChunks }
  }
  const fetchCalls = []
  const resetCalls = []
  const originalGetThreadActiveRun = agentApi.getThreadActiveRun
  agentApi.getThreadActiveRun = async () => {
    throw new Error('network down')
  }

  try {
    const runStream = createRunStream({
      threadState,
      handleStreamChunk: () => {},
      resetOnGoingConv: (threadId, options) => resetCalls.push([threadId, options]),
      fetchThreadMessages: async (payload) => fetchCalls.push(payload)
    })

    await runStream.resumeActiveRunForThread('thread-3')
    await new Promise((resolve) => setTimeout(resolve, 300))

    assert.deepEqual(fetchCalls, [])
    assert.deepEqual(resetCalls, [])
    assert.equal(threadState.onGoingConv.msgChunks, residualChunks)
  } finally {
    agentApi.getThreadActiveRun = originalGetThreadActiveRun
  }
})
