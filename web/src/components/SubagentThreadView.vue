<template>
  <div class="subagent-thread-view">
    <div ref="scrollContainerRef" class="subagent-thread-scroll" @scroll="handleScroll">
      <div ref="contentRef" class="subagent-thread-content">
        <details v-if="currentRunId" class="subagent-run-usage">
          <summary>
            本次运行 Token：{{ formatUsageCount(runTokenUsage?.total?.total_tokens) }}
            <span v-if="runTokenUsage && !runTokenUsage.complete">（部分上报）</span>
          </summary>
          <div v-if="runTokenUsage" class="usage-details">
            <span>输入 {{ formatUsageCount(runTokenUsage.total?.input_tokens) }}</span>
            <span>输出 {{ formatUsageCount(runTokenUsage.total?.output_tokens) }}</span>
            <div v-for="(bucket, key) in runTokenUsage.models" :key="key" class="usage-model">
              <span>{{ bucket.model?.configured_model_spec || key }}</span>
              <span>
                输入 {{ formatUsageCount(bucket.usage?.input_tokens) }} · 输出
                {{ formatUsageCount(bucket.usage?.output_tokens) }}
              </span>
            </div>
          </div>
          <p v-else>本次运行用量暂不可用。</p>
        </details>
        <div v-if="loading && !hasRenderableMessages" class="subagent-thread-state">
          正在加载子智能体消息...
        </div>
        <div v-else-if="error" class="subagent-thread-state is-error">{{ error }}</div>
        <ThreadMessageList
          v-else
          :messages="displayMessages"
          :ongoing-messages="streamedMessages"
          :is-processing="streamActive"
        />
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { agentApi } from '@/apis'
import { processRunSseResponse } from '@/composables/useAgentRunStream'
import { useAgentStreamHandler } from '@/composables/useAgentStreamHandler'
import { useStreamSmoother } from '@/composables/useStreamSmoother'
import ThreadMessageList from '@/components/ThreadMessageList.vue'
import { MessageProcessor } from '@/utils/messageProcessor'
import ScrollController from '@/utils/scrollController'
import { formatContextToken } from '@/utils/contextUsage'
import { getSubagentRunTokenUsage } from '@/utils/subagentRuns'

const props = defineProps({
  threadId: { type: String, required: true },
  active: { type: Boolean, default: false }
})

const RUN_TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled', 'interrupted'])
const loading = ref(false)
const error = ref('')
const messages = ref([])
const currentRunId = ref('')
const currentRunStatus = ref('')
const runTokenUsage = ref(null)
const streamActive = ref(false)
const lastEventId = ref('0-0')
const scrollContainerRef = ref(null)
const contentRef = ref(null)
const streamState = reactive({ threadStates: {} })
let streamAbortController = null
let resizeObserver = null
let reconnectTimer = null
let loadVersion = 0
let disposed = false

const normalizeRunStatus = (status) => String(status || '').trim()
/** 未上报不能显示为零消耗。 */
const formatUsageCount = (value) =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? formatContextToken(value)
    : '未知'
const isTerminalRunStatus = (status) => RUN_TERMINAL_STATUSES.has(normalizeRunStatus(status))
const getStreamThreadState = (threadId) => {
  if (!streamState.threadStates[threadId]) {
    streamState.threadStates[threadId] = {
      isStreaming: false,
      replyLoadingVisible: false,
      pendingRequestId: null,
      pendingInterrupt: null,
      onGoingConv: {
        msgChunks: {},
        currentRequestKey: null,
        currentAssistantKey: null,
        toolCallBuffers: {}
      },
      agentState: null
    }
  }
  return streamState.threadStates[threadId]
}
const streamSmoother = useStreamSmoother({ getThreadState: getStreamThreadState })
const { handleStreamChunk } = useAgentStreamHandler({
  getThreadState: getStreamThreadState,
  processApprovalInStream: () => false,
  currentAgentId: ref(''),
  supportsFiles: ref(false),
  streamSmoother
})
const streamedMessages = computed(() => {
  const threadState = getStreamThreadState(props.threadId)
  const chunks = Object.values(threadState.onGoingConv.msgChunks)
    .map(MessageProcessor.mergeMessageChunk)
    .filter(Boolean)
  return chunks.length
    ? MessageProcessor.convertToolResultToMessages(chunks).filter(
        (message) => message.type !== 'tool'
      )
    : []
})
const displayMessages = computed(() => messages.value)
const hasRenderableMessages = computed(
  () => displayMessages.value.length > 0 || streamedMessages.value.length > 0
)
const scrollController = new ScrollController(() => scrollContainerRef.value, {
  threshold: 80,
  scrollDelay: 80
})
const handleScroll = (event) => {
  scrollController.handleScroll(event)
}

const flattenContent = (content) => {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return content ?? ''
  return content
    .filter((block) => block?.type === 'text')
    .map((block) => block.text || '')
    .join('')
}
const normalizeMessages = (items) =>
  (Array.isArray(items) ? items : []).map((message) => ({
    ...message,
    content: flattenContent(message.content)
  }))
const resetStreamState = () => {
  streamSmoother.resetThread(props.threadId)
  delete streamState.threadStates[props.threadId]
}
const stopRunStream = () => {
  streamAbortController?.abort()
  streamAbortController = null
  streamActive.value = false
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
}
const scrollToBottom = async (force = false) => {
  if (!props.active) return
  await nextTick()
  if (force) await scrollController.scrollToBottomStaticForce()
  else await scrollController.scrollToBottom()
}
const getMessageRunId = (message) => {
  const runId = message?.extra_metadata?.run_id || message?.run_id
  return typeof runId === 'string' ? runId : ''
}
const loadThread = async () => {
  if (!props.threadId) return
  const threadId = props.threadId
  const version = ++loadVersion
  stopRunStream()
  resetStreamState()
  runTokenUsage.value = null
  currentRunId.value = ''
  messages.value = []
  loading.value = true
  error.value = ''
  try {
    const response = await agentApi.getAgentState(threadId, { includeMessages: true })
    if (disposed || version !== loadVersion) return
    const runId = response?.subagent_run?.run_id ? String(response.subagent_run.run_id) : ''
    currentRunId.value = runId
    currentRunStatus.value = normalizeRunStatus(response?.subagent_run?.status)
    const [history, runResponse] = await Promise.all([
      agentApi.getAgentHistory(threadId),
      // 用量暂不可用不阻止已确认 Run 的历史和实时订阅。
      runId ? agentApi.getAgentRun(runId).catch(() => null) : null
    ])
    if (disposed || version !== loadVersion) return
    messages.value = normalizeMessages(history.history || [])
    runTokenUsage.value = getSubagentRunTokenUsage(runResponse?.run, runId, threadId)
    if (runResponse?.run?.id === runId && runResponse.run.conversation_thread_id === threadId) {
      currentRunStatus.value = normalizeRunStatus(runResponse.run.status)
    }
    if (runId && !isTerminalRunStatus(currentRunStatus.value)) {
      messages.value = messages.value.filter((message) => getMessageRunId(message) !== runId)
      lastEventId.value = '0-0'
      void startRunStream(runId, lastEventId.value, true)
    }
    await scrollToBottom(true)
  } catch (loadError) {
    if (disposed || version !== loadVersion) return
    error.value = '加载子智能体消息失败'
    console.error('Failed to load subagent thread messages:', loadError)
  } finally {
    if (version === loadVersion) loading.value = false
  }
}
const scheduleReconnect = (runId) => {
  if (disposed || reconnectTimer) return
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    void startRunStream(runId, lastEventId.value, false)
  }, 1000)
}
const startRunStream = async (runId, afterSeq = '0-0', resetMessages = false) => {
  stopRunStream()
  if (disposed || !runId) return
  if (resetMessages) resetStreamState()
  const controller = new AbortController()
  streamAbortController = controller
  streamActive.value = true
  getStreamThreadState(props.threadId).isStreaming = true

  try {
    const response = await agentApi.streamAgentRunEvents(runId, afterSeq, {
      signal: controller.signal
    })
    if (!response.ok) throw new Error(`SSE response not ok: ${response.status}`)
    await processRunSseResponse(response, (event, data, eventId) => {
      if (!data || controller.signal.aborted || runId !== currentRunId.value) return
      if (data.run_id && data.run_id !== runId) return
      if (eventId) lastEventId.value = String(eventId)
      const payload = data.payload || {}
      const isRetryableError =
        event === 'error' && (payload.retryable === true || payload.chunk?.retryable === true)
      if (isRetryableError) return
      const chunks = Array.isArray(payload.items)
        ? payload.items
        : payload.chunk
          ? [payload.chunk]
          : []
      chunks.forEach((chunk) => {
        if (chunk.run_id && chunk.run_id !== runId) return
        const threadId =
          data.thread_id ||
          payload.thread_id ||
          chunk.thread_id ||
          chunk.meta?.thread_id ||
          chunk.metadata?.thread_id ||
          props.threadId
        if (threadId !== props.threadId) return
        handleStreamChunk(
          {
            ...chunk,
            request_id: chunk.request_id || data.request_id,
            run_id: chunk.run_id || data.run_id || runId,
            thread_id: threadId
          },
          threadId
        )
      })
      if (event === 'end') streamActive.value = false
    })
  } catch (streamError) {
    if (streamError?.name !== 'AbortError') {
      console.error('Failed to stream subagent run messages:', streamError)
    }
  } finally {
    if (streamAbortController === controller) {
      streamAbortController = null
      streamActive.value = false
    }
    if (!controller.signal.aborted && !disposed && runId === currentRunId.value) {
      streamSmoother.flushThread(props.threadId)
      try {
        const runResponse = await agentApi.getAgentRun(runId)
        if (!disposed && !controller.signal.aborted && runId === currentRunId.value) {
          runTokenUsage.value = getSubagentRunTokenUsage(runResponse?.run, runId, props.threadId)
          const status = normalizeRunStatus(runResponse?.run?.status)
          if (isTerminalRunStatus(status)) await loadThread()
          else scheduleReconnect(runId)
        }
      } catch {
        if (!controller.signal.aborted && runId === currentRunId.value) scheduleReconnect(runId)
      }
    }
  }
}

watch(() => props.threadId, loadThread)
watch(
  () => props.active,
  (active) => {
    if (active) scrollToBottom(true)
  }
)
watch(streamedMessages, () => scrollToBottom(), { deep: true, flush: 'post' })

onMounted(() => {
  loadThread()
  if (typeof ResizeObserver !== 'undefined' && contentRef.value) {
    resizeObserver = new ResizeObserver(() => scrollToBottom())
    resizeObserver.observe(contentRef.value)
  }
})
onUnmounted(() => {
  disposed = true
  loadVersion += 1
  stopRunStream()
  resetStreamState()
  resizeObserver?.disconnect()
  scrollController.reset()
})
</script>

<style scoped lang="less">
.subagent-thread-view,
.subagent-thread-scroll {
  width: 100%;
  height: 100%;
  min-height: 0;
}

.subagent-thread-scroll {
  overflow-y: auto;
  padding: 16px 28px 28px;
}

.subagent-thread-content {
  width: min(100%, 800px);
  min-height: 100%;
  margin: 0 auto;
}

.subagent-thread-state {
  padding: 32px 0;
  color: var(--gray-500);
  font-size: 13px;
  text-align: center;

  &.is-error {
    color: var(--color-error-600);
  }
}

.subagent-run-usage {
  margin-bottom: 16px;
  color: var(--color-text-secondary);
  font-size: 12px;

  summary {
    cursor: pointer;
    padding: 8px 0;
  }

  .usage-details {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 16px;
    padding: 8px 0;
  }

  .usage-model {
    width: 100%;
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 4px 12px;
    overflow-wrap: anywhere;
  }
}
</style>
