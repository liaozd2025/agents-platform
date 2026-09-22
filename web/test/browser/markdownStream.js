import { createApp, h, nextTick, ref } from 'vue'
import { createPinia } from 'pinia'
import MarkdownPreview from '../../src/components/common/MarkdownPreview.vue'

/** 固定时钟检查真实 Markdown DOM，避免用机器速度作为节流 oracle。 */
export async function checkMarkdownStream() {
  const host = document.createElement('div')
  document.body.append(host)
  const content = ref('开始'),
    streaming = ref(true)
  const app = createApp({
    render: () => h(MarkdownPreview, { content: content.value, streaming: streaming.value })
  })
  app.use(createPinia())
  const originalSet = window.setTimeout,
    originalClear = window.clearTimeout
  const timers = new Map()
  const settle = async () => {
    await nextTick()
    await new Promise((resolve) => originalSet(resolve, 20))
  }
  let mounted = false
  try {
    app.mount(host)
    mounted = true
    for (let i = 0; i < 100 && host.textContent.trim() !== '开始'; i++) await settle()
    window.setTimeout = (callback, delay, ...args) => {
      if (delay !== 100) return originalSet(callback, delay, ...args)
      const id = Symbol('markdown-timer')
      timers.set(id, () => callback(...args))
      return id
    }
    window.clearTimeout = (id) => {
      if (!timers.delete(id)) originalClear(id)
    }
    for (let i = 0; i < 10; i++) {
      content.value = `正文${i}`
      await settle()
    }
    const waiting = host.textContent.trim(),
      pending = timers.size
    for (const [id, callback] of timers) {
      timers.delete(id)
      callback()
    }
    await settle()
    const flushed = host.textContent.trim()
    content.value = '尾部完整'
    streaming.value = false
    await settle()
    const final = host.textContent.trim()
    streaming.value = true
    content.value = '卸载前缓冲'
    await nextTick()
    app.unmount()
    mounted = false
    return { waiting, pending, flushed, final, remaining: timers.size }
  } finally {
    if (mounted) app.unmount()
    host.remove()
    window.setTimeout = originalSet
    window.clearTimeout = originalClear
  }
}
