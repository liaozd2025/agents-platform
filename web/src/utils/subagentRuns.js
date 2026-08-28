const SUBAGENT_LAUNCH_TOOL_NAMES = new Set(['task', 'subagent_start', 'pi_sandbox'])

/** 判断工具调用是否会启动或继续子智能体运行。 */
export const isSubagentLaunchToolName = (name) => SUBAGENT_LAUNCH_TOOL_NAMES.has(name)

/** 补全任务描述并把同一子线程收敛为一个展示项。 */
export const mergeSubagentRunsForDisplay = (runs, descriptionByToolCallId = new Map()) => {
  if (!Array.isArray(runs)) return []

  const result = []
  const indexByThreadId = new Map()
  const runIdsByThreadId = new Map()
  const toolCallIdsByThreadId = new Map()

  runs.forEach((run) => {
    const toolCallId = run?.id ? String(run.id) : ''
    const stateDescription = String(run?.description || '').trim()
    const taskDescription = toolCallId
      ? String(descriptionByToolCallId.get(toolCallId) || '').trim()
      : ''
    const normalizedRun = {
      ...run,
      description: stateDescription || taskDescription,
      run_count: 1,
      status_counts: { [String(run?.status || 'pending')]: 1 }
    }
    const threadId = run?.child_thread_id ? String(run.child_thread_id) : ''
    const runId = run?.run_id ? String(run.run_id) : ''

    if (!threadId || !indexByThreadId.has(threadId)) {
      if (threadId) {
        indexByThreadId.set(threadId, result.length)
        runIdsByThreadId.set(threadId, new Set(runId ? [runId] : []))
        toolCallIdsByThreadId.set(threadId, new Set(toolCallId ? [toolCallId] : []))
      }
      result.push(normalizedRun)
      return
    }

    const seenRunIds = runIdsByThreadId.get(threadId)
    const seenToolCallIds = toolCallIdsByThreadId.get(threadId)
    if (runId ? seenRunIds.has(runId) : toolCallId && seenToolCallIds.has(toolCallId)) return
    if (runId) seenRunIds.add(runId)
    if (toolCallId) seenToolCallIds.add(toolCallId)

    const index = indexByThreadId.get(threadId)
    const previousRun = result[index]
    const status = String(run?.status || 'pending')
    result[index] = {
      ...previousRun,
      ...normalizedRun,
      description: normalizedRun.description || previousRun.description || '',
      run_count: previousRun.run_count + 1,
      status_counts: {
        ...previousRun.status_counts,
        [status]: (previousRun.status_counts[status] || 0) + 1
      }
    }
  })

  return result.map((run) => {
    const activeCount =
      (run.status_counts.running || 0) +
      (run.status_counts.pending || 0) +
      (run.status_counts.cancel_requested || 0)
    const indicatorStatus = activeCount
      ? 'running'
      : run.status_counts.completed === run.run_count
        ? 'completed'
        : run.status_counts.failed === run.run_count
          ? 'failed'
          : ''
    return {
      ...run,
      description: run.description || String(run?.child_thread_id || run?.id || ''),
      indicator_status: indicatorStatus
    }
  })
}
