import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  isSubagentLaunchToolName,
  mergeSubagentRunsForDisplay
} from '../../src/utils/subagentRuns.js'

test('task、subagent_start 和 pi_sandbox 都属于子智能体启动调用', () => {
  assert.equal(isSubagentLaunchToolName('task'), true)
  assert.equal(isSubagentLaunchToolName('subagent_start'), true)
  assert.equal(isSubagentLaunchToolName('pi_sandbox'), true)
  assert.equal(isSubagentLaunchToolName('subagent_status'), false)
})

test('同一 child_thread_id 收敛为一项但保留真实运行计数', () => {
  const runs = mergeSubagentRunsForDisplay([
    {
      id: 'tool-1',
      run_id: 'run-1',
      child_thread_id: 'child-thread-1',
      description: '整理调研资料',
      status: 'completed'
    },
    {
      id: 'tool-2',
      run_id: 'run-2',
      child_thread_id: 'child-thread-1',
      status: 'running'
    }
  ])

  assert.equal(runs.length, 1)
  assert.equal(runs[0].run_id, 'run-2')
  assert.equal(runs[0].status, 'running')
  assert.equal(runs[0].description, '整理调研资料')
  assert.deepEqual(
    {
      runCount: runs[0].run_count,
      statusCounts: runs[0].status_counts,
      indicatorStatus: runs[0].indicator_status
    },
    {
      runCount: 2,
      statusCounts: { completed: 1, running: 1 },
      indicatorStatus: 'running'
    }
  )
})

test('PI 多次运行不会把失败次数折叠丢失', () => {
  const runs = mergeSubagentRunsForDisplay(
    Array.from({ length: 16 }, (_, index) => ({
      id: `tool-${index}`,
      run_id: `run-${index}`,
      child_thread_id: 'pi-thread-1',
      subagent_slug: 'pi_sandbox',
      status: index < 8 ? 'completed' : 'failed'
    }))
  )

  assert.equal(runs.length, 1)
  assert.equal(runs[0].run_count, 16)
  assert.deepEqual(runs[0].status_counts, { completed: 8, failed: 8 })
  assert.equal(runs[0].indicator_status, '')
})

test('同一工具调用的流式占位不会把 PI 运行重复计数', () => {
  const runs = mergeSubagentRunsForDisplay([
    {
      id: 'tool-1',
      run_id: 'run-1',
      child_thread_id: 'pi-thread-1',
      status: 'completed'
    },
    { id: 'tool-1', child_thread_id: 'pi-thread-1', status: 'running' },
    { id: 'tool-2', child_thread_id: 'pi-thread-1', status: 'running' }
  ])

  assert.equal(runs[0].run_count, 2)
  assert.deepEqual(runs[0].status_counts, { completed: 1, running: 1 })
})

test('不同持久化 Run 即使复用工具调用 ID 也分别计数', () => {
  const runs = mergeSubagentRunsForDisplay([
    {
      id: 'tool-1',
      run_id: 'run-1',
      child_thread_id: 'pi-thread-1',
      status: 'completed'
    },
    {
      id: 'tool-1',
      run_id: 'run-2',
      child_thread_id: 'pi-thread-1',
      status: 'failed'
    }
  ])

  assert.equal(runs[0].run_count, 2)
  assert.deepEqual(runs[0].status_counts, { completed: 1, failed: 1 })
})

test('等待、取消和中断状态不会在聚合时被吞掉', () => {
  const runs = mergeSubagentRunsForDisplay(
    ['pending', 'cancel_requested', 'cancelled', 'interrupted'].map((status, index) => ({
      id: `tool-${index}`,
      child_thread_id: 'child-thread-1',
      status
    }))
  )

  assert.deepEqual(runs[0].status_counts, {
    pending: 1,
    cancel_requested: 1,
    cancelled: 1,
    interrupted: 1
  })
})

test('任务描述从 task 调用回填，缺失时显示 child_thread_id', () => {
  const descriptions = new Map([['tool-1', '生成交付报告']])
  const runs = mergeSubagentRunsForDisplay(
    [
      {
        id: 'tool-1',
        child_thread_id: 'child-thread-1',
        description: '   ',
        status: 'completed'
      },
      { id: 'tool-2', child_thread_id: 'child-thread-2', status: 'completed' }
    ],
    descriptions
  )

  assert.equal(runs[0].description, '生成交付报告')
  assert.equal(runs[1].description, 'child-thread-2')
})

test('异步启动继续同一子线程时显示最新一次输入描述', () => {
  const descriptions = new Map([
    ['start-tool-1', '开始收集资料'],
    ['start-tool-2', '继续整理报告']
  ])
  const runs = mergeSubagentRunsForDisplay(
    [
      {
        id: 'start-tool-1',
        run_id: 'run-1',
        child_thread_id: 'child-thread-1',
        status: 'completed'
      },
      {
        id: 'start-tool-2',
        run_id: 'run-2',
        child_thread_id: 'child-thread-1',
        status: 'running'
      }
    ],
    descriptions
  )

  assert.equal(runs.length, 1)
  assert.equal(runs[0].run_id, 'run-2')
  assert.equal(runs[0].description, '继续整理报告')
})

test('历史会话从 messages 分组收集子智能体任务描述', () => {
  const source = readFileSync(
    new URL('../../src/components/AgentChatComponent.vue', import.meta.url),
    'utf8'
  )
  const collector = source.slice(
    source.indexOf('const subagentDescriptionByToolCallId'),
    source.indexOf('// 先按真实 run')
  )

  assert.match(
    collector,
    /historyConversations\.value\.forEach\(\(conversation\) => collect\(conversation\?\.messages\)\)/
  )
})

test('状态面板区分主 Agent 计划与 Agent 执行统计', () => {
  const source = readFileSync(
    new URL('../../src/components/AgentChatComponent.vue', import.meta.url),
    'utf8'
  )

  assert.match(source, />主 Agent 计划</)
  assert.match(source, /仅反映主 Agent 计划；PI 与子智能体执行进度见下方。/)
  assert.match(source, /\{\{ totalSubagentRunCount \}\}/)
  assert.match(source, /run\.status_counts/)
})

test('尚未获得 child_thread_id 的流式任务不会被误合并', () => {
  const runs = mergeSubagentRunsForDisplay([
    { id: 'tool-1', status: 'running' },
    { id: 'tool-2', status: 'running' }
  ])

  assert.equal(runs.length, 2)
  assert.deepEqual(
    runs.map((run) => run.description),
    ['tool-1', 'tool-2']
  )
})
