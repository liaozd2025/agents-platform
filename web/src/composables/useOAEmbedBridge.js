import { onMounted, onUnmounted, ref, unref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useChatThreadsStore } from '@/stores/chatThreads'
import { useProjectsStore } from '@/stores/projects'
import { useUserStore } from '@/stores/user'
import { authApi } from '@/apis/auth_api'
import {
  confirmEmbedDisplayMode,
  resolveAppNavigationPath,
  resetEmbedDisplayMode
} from '@/composables/useEmbedMode'
import { canAccessRoute, getAuthenticatedHomePath } from '@/utils/authNavigation'
import {
  createOAEmbedBridge,
  getOAEmbedRenewalDelay,
  getOAEmbedRenewalTimerDelay,
  parseOAEmbedAllowedOrigins
} from '@/utils/oaEmbedBridge'
import { setOAEmbedAuthRequiredHandler, setOAEmbedNavigateHandler } from '@/utils/oaEmbedSession'

// 仅用于定位 iframe 生命周期问题：模块级计数可以区分组件重建与同一实例重复启动。
let embedBridgeInstanceCount = 0
let embedBridgeStartCount = 0
let embedBridgeStopCount = 0
/** 在嵌入路由中将父项目下发的 OA 账号交换为 Yuxi 登录态。 */
export function useOAEmbedBridge(enabled) {
  const instanceId = ++embedBridgeInstanceCount
  const userStore = useUserStore()
  const chatThreadsStore = useChatThreadsStore()
  const projectsStore = useProjectsStore()
  const route = useRoute()
  const router = useRouter()
  const isAuthorized = ref(false)
  const statusMessage = ref('等待 OA 授权')
  let bridge = null
  let clearAuthRequiredHandler = null
  let clearNavigateHandler = null
  let renewalTimer = null

  const clearRenewalTimer = () => {
    if (renewalTimer !== null) window.clearTimeout(renewalTimer)
    renewalTimer = null
  }

  const scheduleRenewal = (accessToken) => {
    clearRenewalTimer()
    const delay = getOAEmbedRenewalDelay(accessToken)
    if (delay === null) return
    // 记录实际等待时间，便于确认短有效期 token 不会在换票后立刻触发 logout/abort。
    console.info('[OA iframe][诊断] 已安排 token 续期', { delayMs: delay })
    const timerDelay = getOAEmbedRenewalTimerDelay(delay)
    if (timerDelay === null) return
    if (timerDelay < delay) {
      // 长 token 先等待一个安全的分段时长，回调中重新计算剩余时间，避免 setTimeout 溢出。
      renewalTimer = window.setTimeout(() => scheduleRenewal(accessToken), timerDelay)
      return
    }
    renewalTimer = window.setTimeout(requestAuthRequired, timerDelay)
  }

  const clearAuthorization = (message) => {
    isAuthorized.value = false
    statusMessage.value = message
    userStore.logout()
    chatThreadsStore.reset()
    projectsStore.reset()
  }

  const requestAuthRequired = () => {
    clearRenewalTimer()
    clearAuthorization('等待 OA 重新授权')
    bridge?.requestAuthRequired()
  }

  const startBridge = () => {
    embedBridgeStartCount += 1
    console.info('[OA iframe][诊断] startBridge', {
      instanceId,
      startCount: embedBridgeStartCount,
      path: window.location.pathname,
      hasBridge: Boolean(bridge),
      enabled: Boolean(unref(enabled))
    })
    if (bridge) return
    if (!unref(enabled)) {
      isAuthorized.value = true
      return
    }
    resetEmbedDisplayMode()

    const allowedOrigins = parseOAEmbedAllowedOrigins(
      import.meta.env.VITE_YUXI_EMBED_ALLOWED_ORIGINS
    )
    if (!allowedOrigins.length) {
      statusMessage.value = 'OA 嵌入未配置'
      return
    }

    bridge = createOAEmbedBridge({
      allowedOrigins,
      onAccount: async (account) => {
        // 换票成功前不登出旧会话，避免 abort 认证控制器并取消正在加载的业务接口。
        try {
          const loginData = await authApi.exchangeOAAccount(account)
          await userStore.acceptEmbedToken(loginData.access_token)
          scheduleRenewal(loginData.access_token)
          if (!canAccessRoute(route.matched, userStore.hasPermission)) {
            await router.replace(
              resolveAppNavigationPath(true, getAuthenticatedHomePath(userStore.hasPermission))
            )
          }
          isAuthorized.value = true
          statusMessage.value = ''
        } catch (error) {
          isAuthorized.value = false
          statusMessage.value = '等待 OA 重新授权'
          throw error
        }
      },
      onModeChanged: confirmEmbedDisplayMode
    })
    clearAuthRequiredHandler = setOAEmbedAuthRequiredHandler(requestAuthRequired)
    // 来源列表等深层组件通过该通道请求父页面做 OA 内部跳转；返回 false 时由调用方降级为新标签。
    clearNavigateHandler = setOAEmbedNavigateHandler((params) => {
      if (!bridge) return false
      return bridge.requestNavigate(params)
    })
    bridge.start()
  }

  const stopBridge = (clearSession = false) => {
    embedBridgeStopCount += 1
    console.info('[OA iframe][诊断] stopBridge', {
      instanceId,
      stopCount: embedBridgeStopCount,
      path: window.location.pathname,
      clearSession,
      hasBridge: Boolean(bridge)
    })
    clearRenewalTimer()
    bridge?.stop()
    bridge = null
    clearAuthRequiredHandler?.()
    clearAuthRequiredHandler = null
    clearNavigateHandler?.()
    clearNavigateHandler = null
    // keep-alive 切换只暂停桥接监听，真正卸载时才清理登录态。
    if (clearSession && unref(enabled)) {
      clearAuthorization('等待 OA 授权')
    }
  }

  onMounted(startBridge)
  onUnmounted(() => stopBridge(true))

  return {
    isAuthorized,
    statusMessage,
    requestDisplayMode(mode, threadId) {
      // 正式父插件没有模式确认回执，消息发出后由当前嵌入页面立即完成状态同步。
      if (bridge?.requestMode(mode, threadId)) confirmEmbedDisplayMode(mode)
    },
    requestClose(threadId) {
      bridge?.requestClose(threadId)
    }
  }
}
