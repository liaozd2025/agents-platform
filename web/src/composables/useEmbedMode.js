import { computed } from 'vue'
import { useRoute } from 'vue-router'

/** 提供当前页面的全局运行形态，独立站不会暴露 OA 控件。 */
export function useEmbedContext() {
  const route = useRoute()
  const isEmbedded = computed(() => route.matched.some((record) => record.meta.embed === true))
  const surface = computed(() => (isEmbedded.value ? 'oa-embed' : 'standalone'))

  return { surface, isEmbedded }
}
