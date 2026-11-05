import assert from 'node:assert/strict'
import test from 'node:test'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createTestServer } from '../helpers/createTestServer.js'

test('上游概览卡片区分加载、失败和真实数值，不把失败显示为零', async () => {
  const server = await createTestServer({ server: { middlewareMode: true } })
  try {
    const { default: Overview } = await server.ssrLoadModule('/src/modules/dashboard/ui/StatsOverviewComponent.vue')
    const basicStats = { total_sessions: 12, active_sessions: 5, total_messages: 60, total_users: 3 }
    for (const [props, expected] of [
      [{ basicStatsLoading: true }, ['…', '…', '…', '…']],
      [{ basicStatsError: true }, ['—', '—', '—', '—']],
      [{}, ['12', '5', '60', '3']]
    ]) {
      const html = await renderToString(createSSRApp({ render: () => h(Overview, { basicStats, ...props }) }))
      const values = [...html.matchAll(/class="dashboard-metric-value"[^>]*>([^<]*)</g)].map(match => match[1])
      assert.deepEqual(values.slice(0, 4), expected)
      if (props.basicStatsError) assert.match(html, /统计数据加载失败[\s\S]*重试/)
    }
  } finally { await server.close() }
})
