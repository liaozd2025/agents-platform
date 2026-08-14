import { computed, readonly, ref } from 'vue'
import { useRoute } from 'vue-router'
import { DEFAULT_OA_EMBED_MODE, OA_EMBED_MODES } from '../utils/oaEmbedBridge.js'

const displayMode = ref(DEFAULT_OA_EMBED_MODE)
const modeConfirmed = ref(false)
const readonlyDisplayMode = readonly(displayMode)
const readonlyModeConfirmed = readonly(modeConfirmed)
const embedAppSections = ['/agent-manage', '/workspace', '/extensions', '/dashboard']

/** 根据路由元数据解析应用运行形态。 */
export function resolveAppSurface(route) {
  return route.matched.some((record) => record.meta.embed === true) ? 'oa-embed' : 'standalone'
}

/** 独立站始终显示侧栏，OA 嵌入仅在全屏模式显示。 */
export function shouldShowAppSidebar(isEmbedded, mode) {
  return !isEmbedded || mode === 'fullscreen'
}

/** 将 OA 全屏中的 PC 功能导航保留在嵌入路由内。 */
export function resolveAppNavigationPath(isEmbedded, path) {
  if (!isEmbedded) return path
  if (path === '/agent') return '/embed'
  return embedAppSections.some((section) => path === section || path.startsWith(`${section}/`))
    ? `/embed${path}`
    : path
}

/** 开始新的嵌入显示会话，等待父页面确认默认固定模式。 */
export function resetEmbedDisplayMode() {
  displayMode.value = DEFAULT_OA_EMBED_MODE
  modeConfirmed.value = false
}

/** 标记显示模式请求正在等待父页面确认。 */
export function markEmbedDisplayModePending() {
  modeConfirmed.value = false
}

/** 使用父页面确认的合法模式更新全局嵌入上下文。 */
export function confirmEmbedDisplayMode(mode) {
  if (!OA_EMBED_MODES.includes(mode)) return false
  displayMode.value = mode
  modeConfirmed.value = true
  return true
}

/** 提供当前页面的全局运行形态，独立站不会暴露 OA 控件。 */
export function useEmbedContext() {
  const route = useRoute()
  const surface = computed(() => resolveAppSurface(route))
  const isEmbedded = computed(() => surface.value === 'oa-embed')

  return {
    surface,
    isEmbedded,
    displayMode: readonlyDisplayMode,
    modeConfirmed: readonlyModeConfirmed
  }
}
