/** 解析并校验 OA iframe 的精确 HTTP origin 白名单。 */
export function parseOAEmbedAllowedOrigins(value) {
  return [
    ...new Set(
      String(value || '')
        .split(/[\s,]+/)
        .filter(Boolean)
        .filter((candidate) => {
          try {
            const url = new URL(candidate)
            return ['http:', 'https:'].includes(url.protocol) && url.origin === candidate
          } catch {
            return false
          }
        })
    )
  ]
}

const OA_EMBED_MODES = new Set(['fixed', 'floating', 'fullscreen'])

/**
 * 创建 OA iframe 的 postMessage 桥，所有出站消息都绑定到明确 origin。
 */
export function createOAEmbedBridge({
  allowedOrigins,
  onToken,
  onModeChanged = () => {},
  browserWindow = window,
  setTimer = setTimeout,
  clearTimer = clearTimeout
}) {
  const origins = [...new Set(allowedOrigins)]
  let parentOrigin = ''
  let readyTimer = null
  let tokenAccepted = false

  const post = (type, payload = {}) => {
    const targets = parentOrigin ? [parentOrigin] : origins
    targets.forEach((targetOrigin) => {
      browserWindow.parent.postMessage({ type, ...payload }, targetOrigin)
    })
  }

  const handleMessage = async (event) => {
    if (event.source !== browserWindow.parent || !origins.includes(event.origin)) return

    if (event.data?.type === 'oa:mode-changed') {
      if (event.origin === parentOrigin && OA_EMBED_MODES.has(event.data.mode)) {
        onModeChanged(event.data.mode)
      }
      return
    }

    if (
      event.data?.type !== 'oa:token' ||
      typeof event.data.token !== 'string' ||
      !event.data.token
    )
      return

    parentOrigin = event.origin
    try {
      await onToken(event.data.token)
      tokenAccepted = true
      if (readyTimer !== null) clearTimer(readyTimer)
    } catch {
      post('yuxi:auth-required')
    }
  }

  return {
    start() {
      browserWindow.addEventListener('message', handleMessage)
      post('yuxi:ready')
      readyTimer = setTimer(() => {
        if (!tokenAccepted) post('yuxi:ready')
      }, 3000)
    },
    stop() {
      browserWindow.removeEventListener('message', handleMessage)
      if (readyTimer !== null) clearTimer(readyTimer)
    },
    requestAuthRequired() {
      tokenAccepted = false
      post('yuxi:auth-required')
    },
    requestMode(mode, threadId) {
      if (!OA_EMBED_MODES.has(mode)) return false
      post('yuxi:mode-request', { mode, ...(threadId ? { threadId } : {}) })
      return true
    },
    requestClose(threadId) {
      post('yuxi:close-request', threadId ? { threadId } : {})
    }
  }
}
