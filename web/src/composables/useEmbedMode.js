import { computed } from 'vue'
import { useRoute } from 'vue-router'

/** 判断一组已匹配路由记录是否启用了 OA 嵌入模式。 */
export function routeUsesEmbedMode(route) {
  return route.matched.some((record) => record.meta.embed === true)
}

/** 返回当前路由是否处于 OA 嵌入模式。 */
export function useEmbedMode() {
  const route = useRoute()
  return computed(() => routeUsesEmbedMode(route))
}
