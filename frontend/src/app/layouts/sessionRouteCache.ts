import { ref } from 'vue'
import type { RouteLocationNormalizedLoaded, Router } from 'vue-router'

/** 首条输入接收后把新建视图交给正式路由，其他会话继续独立保活。 */
export function createSessionRouteCache(router: Router) {
  const draftGeneration = ref(0)
  const adoptedKeys = new Map<string, string>()
  let pendingAdoption: { path: string; key: string } | null = null

  const dispose = router.afterEach((to, _from, failure) => {
    if (failure || !pendingAdoption || (to.name === 'AgentComp' || to.name === 'EmbedAgent')) return
    if (to.path === pendingAdoption.path) {
      adoptedKeys.set(to.path, pendingAdoption.key)
      draftGeneration.value += 1
    }
    pendingAdoption = null
  })

  /** URL 改名不改变创建该会话的视图身份。 */
  function key(route: Pick<RouteLocationNormalizedLoaded, 'name' | 'path'>): string {
    return (route.name === 'AgentComp' || route.name === 'EmbedAgent')
      ? `${route.name === 'EmbedAgent' ? 'embed-' : ''}session-draft:${draftGeneration.value}`
      : adoptedKeys.get(route.path) || route.path
  }

  /** 导航成功才分配下一张新建页，取消或异常保留当前草稿。 */
  async function adopt(threadId: string): Promise<void> {
    if (router.currentRoute.value.name !== 'AgentComp' && router.currentRoute.value.name !== 'EmbedAgent') return
    const target = router.resolve({ name: router.currentRoute.value.name === 'EmbedAgent' ? 'EmbedAgentWithThreadId' : 'AgentCompWithThreadId', params: { thread_id: threadId } })
    pendingAdoption = { path: target.path, key: key(router.currentRoute.value) }
    // 交接归属由成功导航闭合；原 replace 可能被同目标、仍在等待的后继导航取消。
    await router.replace(target)
  }

  return { key, adopt, dispose }
}
