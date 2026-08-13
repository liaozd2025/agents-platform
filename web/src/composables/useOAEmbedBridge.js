import { onActivated, onDeactivated, onMounted, onUnmounted, ref, unref } from 'vue'
import { useChatThreadsStore } from '@/stores/chatThreads'
import { useUserStore } from '@/stores/user'
import { authApi } from '@/apis/auth_api'
import {
  confirmEmbedDisplayMode,
  markEmbedDisplayModePending,
  resetEmbedDisplayMode
} from '@/composables/useEmbedMode'
import { createOAEmbedBridge, parseOAEmbedAllowedOrigins } from '@/utils/oaEmbedBridge'
import { setOAEmbedAuthRequiredHandler } from '@/utils/oaEmbedSession'

/** 在嵌入路由中将 OA 现有 token 交换为 Yuxi 登录态。 */
export function useOAEmbedBridge(enabled) {
  const userStore = useUserStore()
  const chatThreadsStore = useChatThreadsStore()
  const isAuthorized = ref(false)
  const statusMessage = ref('等待 OA 授权')
  let bridge = null
  let clearAuthRequiredHandler = null

  const clearAuthorization = (message) => {
    isAuthorized.value = false
    statusMessage.value = message
    userStore.logout()
    chatThreadsStore.reset()
  }

  const requestAuthRequired = () => {
    clearAuthorization('等待 OA 重新授权')
    bridge?.requestAuthRequired()
  }

  const startBridge = () => {
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
      onToken: async (token) => {
        clearAuthorization('正在验证 OA 授权')
        try {
          const loginData = await authApi.exchangeOAToken(token)
          await userStore.acceptEmbedToken(loginData.access_token)
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
    bridge.start()
  }

  const stopBridge = () => {
    bridge?.stop()
    bridge = null
    clearAuthRequiredHandler?.()
    clearAuthRequiredHandler = null
    if (unref(enabled)) {
      clearAuthorization('等待 OA 授权')
    }
  }

  onMounted(startBridge)
  onActivated(startBridge)
  onDeactivated(stopBridge)
  onUnmounted(stopBridge)

  return {
    isAuthorized,
    statusMessage,
    requestDisplayMode(mode, threadId) {
      if (bridge?.requestMode(mode, threadId)) markEmbedDisplayModePending()
    },
    requestClose(threadId) {
      bridge?.requestClose(threadId)
    }
  }
}
