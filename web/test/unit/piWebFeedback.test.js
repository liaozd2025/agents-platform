import assert from 'node:assert/strict'
import path from 'node:path'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createServer } from 'vite'
import { MessageProcessor } from '../../src/utils/messageProcessor.js'
import { getSubagentRunTokenUsage } from '../../src/utils/subagentRuns.js'
import { getToolApprovalSummary } from '../../src/utils/toolApproval.js'

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
let server
let useAgentStreamHandler
let getToolCallStatus
let getToolName
let agentApi
let SubagentThreadView

before(async () => {
  globalThis.localStorage = { getItem: () => null }
  server = await createServer({ root: webRoot, server: { middlewareMode: true } })
  ;({ useAgentStreamHandler } = await server.ssrLoadModule(
    '/src/composables/useAgentStreamHandler.js'
  ))
  ;({ getToolCallStatus, getToolName } = await server.ssrLoadModule(
    '/src/components/ToolCallingResult/toolRegistry.js'
  ))
  ;({ agentApi } = await server.ssrLoadModule('/src/apis/index.js'))
  ;({ default: SubagentThreadView } = await server.ssrLoadModule(
    '/src/components/SubagentThreadView.vue'
  ))
})

after(async () => {
  await server?.close()
  delete globalThis.localStorage
})

/** 按既有消息消费者读取展示结果，不直接断言内部数组长度代替正文。 */
const displayedMessages = (state) =>
  MessageProcessor.convertToolResultToMessages(
    Object.values(state.onGoingConv.msgChunks).map(MessageProcessor.mergeMessageChunk)
  ).filter((message) => message.type === 'ai')

test('首块已带完整 tool_calls 时不再次拼接同块参数', () => {
  const tool = { index: 0, id: 'tool-1', function: { name: 'bash', arguments: '{"command":"ls"}' } }
  const merged = MessageProcessor.mergeMessageChunk([
    {
      type: 'AIMessageChunk',
      id: 'message-1',
      tool_calls: [tool],
      tool_call_chunks: [{ index: 0, id: 'tool-1', name: 'bash', args: '{"command":"ls"}' }]
    }
  ])
  assert.deepEqual(merged.tool_calls, [tool])
})

test('PI 工具累计快照替换正文并保持 running，最终结果覆盖快照与状态', () => {
  const state = { onGoingConv: { msgChunks: {} } }
  const { handleStreamChunk } = useAgentStreamHandler({ getThreadState: () => state })
  for (const runId of ['child-1', 'child-2']) {
    const toolId = `${runId}:call-1`
    handleStreamChunk(
      {
        status: 'loading',
        run_id: runId,
        stream_event: {
          type: 'tool_call',
          message_id: `${runId}:message-1`,
          tool_call_id: toolId,
          name: 'bash',
          args: { command: 'printf example' }
        }
      },
      'thread-1'
    )
    const output = (event, content, status) =>
      handleStreamChunk(
        {
          status: 'stream_event',
          run_id: runId,
          event: {
            method: 'tools',
            data: {
              event,
              output: {
                id: event === 'tool-finished' ? `${runId}:result` : toolId,
                tool_call_id: toolId,
                name: 'bash',
                content,
                status
              }
            }
          }
        },
        'thread-1'
      )
    output('tool-progress', 'first', 'running')
    output('tool-progress', 'first\nsecond', 'running')
    let tool = displayedMessages(state).find((message) => message.run_id === runId).tool_calls[0]
    assert.equal(tool.function.arguments, '{"command":"printf example"}')
    assert.equal(tool.tool_call_result.content, 'first\nsecond')
    assert.equal(getToolCallStatus(tool), 'running')
    output('tool-finished', `${runId}: final`, runId === 'child-1' ? 'success' : 'error')
    tool = displayedMessages(state).find((message) => message.run_id === runId).tool_calls[0]
    assert.equal(tool.tool_call_result.content, `${runId}: final`)
    assert.equal(getToolCallStatus(tool), runId === 'child-1' ? 'completed' : 'error')
  }
  assert.deepEqual(
    displayedMessages(state).map((message) => message.tool_calls[0].tool_call_result.content),
    ['child-1: final', 'child-2: final']
  )
})

test('重复 provider 工具 ID 按 Run 归属关联，PI 正文增量不拼入父消息', () => {
  const state = { onGoingConv: { msgChunks: {} } }
  const { handleStreamChunk } = useAgentStreamHandler({ getThreadState: () => state })
  for (const [runId, text] of [
    ['parent', '父回答'],
    ['child', 'PI 正文']
  ]) {
    for (const content of [text, '完成']) {
      handleStreamChunk(
        {
          status: 'loading',
          run_id: runId,
          stream_event: {
            type: 'message_delta',
            message_id: `${runId}:message`,
            content
          }
        },
        'thread-1'
      )
    }
    handleStreamChunk(
      {
        status: 'loading',
        run_id: runId,
        stream_event: {
          type: 'tool_call',
          message_id: `${runId}:message`,
          tool_call_id: 'same-provider-id',
          name: 'bash',
          args: {}
        }
      },
      'thread-1'
    )
    handleStreamChunk(
      {
        status: 'stream_event',
        run_id: runId,
        event: {
          method: 'tools',
          data: {
            event: 'tool-progress',
            output: {
              tool_call_id: 'same-provider-id',
              content: text,
              status: 'running'
            }
          }
        }
      },
      'thread-1'
    )
  }
  const messages = displayedMessages(state)
  assert.deepEqual(
    messages.map((item) => item.content),
    ['父回答完成', 'PI 正文完成']
  )
  assert.deepEqual(
    messages.map((item) => item.tool_calls[0].tool_call_result.content),
    ['父回答', 'PI 正文']
  )
})

test('PI 的已让位、取消和中断不显示成功，交付工具具有中文名称', () => {
  for (const [status, stopReason, expected] of [
    ['completed', 'steer', 'steered'],
    ['cancelled', null, 'cancelled'],
    ['interrupted', null, 'interrupted'],
    ['failed', null, 'error']
  ]) {
    assert.equal(
      getToolCallStatus({
        name: 'pi_sandbox',
        status: 'success',
        tool_call_result: { content: '结果' },
        subagent_run: { status, stop_reason: stopReason }
      }),
      expected
    )
  }
  assert.equal(
    getToolCallStatus({ tool_call_result: { status: 'error', content: '失败' } }),
    'error'
  )
  assert.equal(getToolName('submit_artifact'), '交付文件')
  assert.equal(getToolCallStatus({ name: 'pi_sandbox', result: '历史结果' }), 'completed')
  assert.equal(
    getToolApprovalSummary({ name: 'pi_sandbox', args: { description: '整理项目报告' } }),
    '整理项目报告'
  )
})

const reportedUsage = {
  schema_version: 2,
  complete: true,
  model_call_count: 1,
  usage_reported_call_count: 1,
  total: { input_tokens: 1200, output_tokens: 34, total_tokens: 1234 },
  models: {
    'provider/model': {
      model: { configured_model_spec: 'provider/model' },
      usage: { input_tokens: 1200, output_tokens: 34, total_tokens: 1234 }
    }
  }
}

test('缺失上报、错误 Run 或线程及父 checkpoint 用量均不能伪装成本次用量', () => {
  const run = {
    id: 'child-run',
    conversation_thread_id: 'child-thread',
    token_usage: reportedUsage
  }
  assert.equal(getSubagentRunTokenUsage(run, 'child-run', 'child-thread'), reportedUsage)
  const partialUsage = { ...reportedUsage, complete: false }
  assert.equal(
    getSubagentRunTokenUsage({ ...run, token_usage: partialUsage }, 'child-run', 'child-thread'),
    partialUsage
  )
  assert.equal(getSubagentRunTokenUsage(run, 'other-run', 'child-thread'), null)
  assert.equal(getSubagentRunTokenUsage(run, 'child-run', 'other-thread'), null)
  for (const usage of [
    {},
    { thread: reportedUsage },
    { ...reportedUsage, usage_reported_call_count: 0 }
  ]) {
    assert.equal(
      getSubagentRunTokenUsage({ ...run, token_usage: usage }, 'child-run', 'child-thread'),
      null
    )
  }
})

test('子线程组件按 state 指定 Run 调用 API 并显示本次用量，切线程时丢弃旧请求', async () => {
  let view
  const props = { threadId: 'child-thread', active: false }
  await renderToString(
    createSSRApp({
      setup() {
        view = SubagentThreadView.setup(props, { expose: () => {} })
        return () => null
      }
    })
  )
  const originals = { ...agentApi }
  const requestedRuns = []
  let resolveOldHistory
  agentApi.getAgentState = async (threadId) => ({
    subagent_run: { run_id: `${threadId}:run`, status: 'completed' },
    token_usage: { thread: { ...reportedUsage, total: { total_tokens: 99999 } } }
  })
  agentApi.getAgentHistory = async (threadId) => ({ history: [{ type: 'ai', content: threadId }] })
  agentApi.getAgentRun = async (runId) => {
    requestedRuns.push(runId)
    return {
      run: {
        id: runId,
        conversation_thread_id: runId.replace(':run', ''),
        status: 'completed',
        token_usage: runId.startsWith('child-thread') ? reportedUsage : {}
      }
    }
  }
  try {
    await view.loadThread()
    assert.deepEqual(requestedRuns, ['child-thread:run'])
    assert.equal(view.runTokenUsage.value.total.total_tokens, 1234)
    assert.equal(view.formatUsageCount(view.runTokenUsage.value.total.total_tokens), '1.23K')
    assert.equal(view.formatUsageCount(undefined), '未知')
    assert.equal(view.formatUsageCount(0), '0')
    agentApi.getAgentHistory = (threadId) =>
      threadId === 'child-thread'
        ? new Promise((resolve) => {
            resolveOldHistory = resolve
          })
        : Promise.resolve({ history: [{ type: 'ai', content: threadId }] })
    const oldLoad = view.loadThread()
    await Promise.resolve()
    props.threadId = 'next-thread'
    await view.loadThread()
    resolveOldHistory({ history: [{ type: 'ai', content: 'stale' }] })
    await oldLoad
    assert.equal(view.currentRunId.value, 'next-thread:run')
    assert.equal(view.runTokenUsage.value, null)
    assert.equal(view.messages.value[0].content, 'next-thread')
  } finally {
    Object.assign(agentApi, originals)
  }
})

test('Run 用量接口失败时保留历史并继续订阅 state 确认的当前 Run', async () => {
  let view
  await renderToString(
    createSSRApp({
      setup() {
        view = SubagentThreadView.setup(
          { threadId: 'child-thread', active: false },
          { expose: () => {} }
        )
        return () => null
      }
    })
  )
  const originals = { ...agentApi }
  const requestedRuns = []
  const streamRuns = []
  agentApi.getAgentState = async () => ({
    subagent_run: { run_id: 'current-child-run', status: 'running' }
  })
  agentApi.getAgentHistory = async () => ({
    history: [{ type: 'ai', content: '此前任务的真实结果', run_id: 'previous-child-run' }]
  })
  agentApi.getAgentRun = async (runId) => {
    requestedRuns.push(runId)
    throw Object.assign(new Error('temporary unavailable'), { response: { status: 503 } })
  }
  agentApi.streamAgentRunEvents = (runId, _afterSeq, { signal }) => {
    streamRuns.push(runId)
    return new Promise((_, reject) =>
      signal.addEventListener(
        'abort',
        () => {
          reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
        },
        { once: true }
      )
    )
  }
  try {
    await view.loadThread()
    assert.equal(view.error.value, '')
    assert.equal(view.messages.value[0].content, '此前任务的真实结果')
    assert.equal(view.runTokenUsage.value, null)
    assert.equal(view.formatUsageCount(view.runTokenUsage.value?.total?.total_tokens), '未知')
    assert.equal(view.currentRunStatus.value, 'running')
    assert.deepEqual(requestedRuns, ['current-child-run'])
    assert.deepEqual(streamRuns, ['current-child-run'])
    assert.equal(view.streamActive.value, true)
  } finally {
    view.stopRunStream()
    await Promise.resolve()
    Object.assign(agentApi, originals)
  }
})
