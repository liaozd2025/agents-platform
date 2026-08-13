import { onActivated, onDeactivated, onMounted, onUnmounted, ref, unref } from 'vue'
import { useChatThreadsStore } from '@/stores/chatThreads'
import { useUserStore } from '@/stores/user'
import { createOAEmbedBridge, parseOAEmbedAllowedOrigins } from '@/utils/oaEmbedBridge'
import { setOAEmbedAuthRequiredHandler } from '@/utils/oaEmbedSession'

/** 在嵌入路由中等待 OA 下发 Yuxi bearer，并维护授权状态。 */
export function useOAEmbedBridge(enabled) {
  const userStore = useUserStore()
  const chatThreadsStore = useChatThreadsStore()
  const isAuthorized = ref(false)
  const statusMessage = ref('等待 OA 授权')
  let bridge = null
  let clearAuthRequiredHandler = null

  const requestAuthRequired = () => {
    isAuthorized.value = false
    statusMessage.value = '等待 OA 重新授权'
    userStore.logout()
    chatThreadsStore.reset(true)
    bridge?.requestAuthRequired()
  }

  const startBridge = () => {
    if (bridge) return
    if (!unref(enabled)) {
      isAuthorized.value = true
      return
    }

    const allowedOrigins = parseOAEmbedAllowedOrigins(
      import.meta.env.VITE_YUXI_EMBED_ALLOWED_ORIGINS
    )
    if (!allowedOrigins.length) {
      statusMessage.value = 'OA 嵌入未配置'
      return
    }

    bridge = createOAEmbedBridge({
      allowedOrigins,
      onToken: async (token) => {
        isAuthorized.value = false
        statusMessage.value = '正在验证 OA 授权'
        userStore.logout()
        chatThreadsStore.reset(true)
        try {
          await userStore.acceptEmbedToken(token)
          isAuthorized.value = true
          statusMessage.value = ''
        } catch (error) {
          isAuthorized.value = false
          statusMessage.value = '等待 OA 重新授权'
          throw error
        }
      }
    })
    clearAuthRequiredHandler = setOAEmbedAuthRequiredHandler(requestAuthRequired)
    bridge.start()
  }

  const stopBridge = () => {
    bridge?.stop()
    bridge = null
    clearAuthRequiredHandler?.()
    clearAuthRequiredHandler = null
    if (unref(enabled)) {
      isAuthorized.value = false
      statusMessage.value = '等待 OA 授权'
      userStore.logout()
      chatThreadsStore.reset(true)
    }
  }

  onMounted(startBridge)
  onActivated(startBridge)
  onDeactivated(stopBridge)
  onUnmounted(stopBridge)

  return {
    isAuthorized,
    statusMessage,
    sendExpand: (threadId) => bridge?.expand(threadId)
  }
}
