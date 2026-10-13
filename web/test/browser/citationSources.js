import { createApp, h } from 'vue'
import { createPinia } from 'pinia'
import Antd from 'ant-design-vue'
import RefsComponent from '../../src/components/RefsComponent.vue'
import MarkdownPreview from '../../src/components/common/MarkdownPreview.vue'
import { getMessageCitationSources } from '../../src/utils/answerCitations.js'
import '../../src/assets/css/base.css'
import '../../src/assets/css/base.dark.css'

const fixture = {
  content:
    '说明书内容<cite source="kb://kb-a/file-1">8</cite>，再引用知识库说明<cite source="kb://kb-b/file-2">2</cite>。',
  extra_metadata: {
    citation_sources: [
      {
        source: 'kb://kb-b/file-2',
        source_type: 'file',
        title: '新九典视界知识库内容',
        kb_id: 'kb-b',
        file_id: 'file-2',
        excerpts: [{ text: '知识库真实原文' }]
      },
      {
        source: 'kb://kb-a/file-1',
        source_type: 'file',
        title: '说明书.pdf',
        kb_id: 'kb-a',
        file_id: 'file-1',
        excerpts: [{ text: '说明书真实原文' }]
      },
      {
        source: 'kb://kb-unused/file-3',
        source_type: 'file',
        title: '未引用候选.pdf',
        kb_id: 'kb-unused',
        file_id: 'file-3',
        excerpts: [{ text: '未采用的候选原文' }]
      }
    ]
  }
}

const waitFor = async (predicate) => {
  for (let i = 0; i < 100; i++) {
    await new Promise((resolve) => setTimeout(resolve, 20))
    const result = predicate()
    if (result) return result
  }
  throw new Error('等待引用来源页面就绪超时')
}

/** 在隔离页面装配真实正文与来源组件，供浏览器回归和历史消息回放。 */
export function mountCitationSources(message = fixture) {
  const host = document.createElement('div')
  host.id = 'citation-source-probe'
  host.style.cssText = 'position:fixed;inset:0;z-index:1000;overflow:auto;background:var(--gray-0)'
  document.body.append(host)
  const app = createApp({
    render: () => h('main', {
      style: 'max-width:760px;margin:24px auto;padding:16px;background:var(--gray-0);color:var(--gray-900)'
    }, [
      h(MarkdownPreview, { content: message.content, citationSources: getMessageCitationSources(message) }),
      h(RefsComponent, { message: { ...message, role: 'assistant', status: 'finished' }, showRefs: ['sources'] })
    ])
  })
  app.use(createPinia()).use(Antd).mount(host)
  return () => {
    app.unmount()
    host.remove()
  }
}

/** 核对引用顺序、候选过滤，并将页面留给 runner 截图及验证点击。 */
export async function checkCitationSources() {
  const unmount = mountCitationSources()
  window.unmountCitationSources = unmount
  try {
    const preview = await waitFor(() => document.querySelector('.yk-markdown-preview .citation-ref'))
    if (preview.dataset.citationSource !== 'kb://kb-a/file-1') {
      throw new Error('正文第一个引用未绑定到说明书.pdf')
    }
    document.querySelector('.sources-btn')?.click()
    const rows = await waitFor(() => {
      const elements = [...document.querySelectorAll('.cited-source')]
      return elements.length === 2 ? elements : null
    })
    const labels = rows.map((row) => {
      const [number, title] = row.querySelectorAll('span')
      return `${number.textContent.trim()} ${title.textContent.trim()}`
    })
    if (labels[0] !== '1 说明书.pdf' || labels[1] !== '2 新九典视界知识库内容') {
      throw new Error(`来源顺序或标题错误：${labels.join(' / ')}`)
    }
    return { labels }
  } catch (error) {
    unmount()
    delete window.unmountCitationSources
    throw error
  }
}

/** 点击第二条来源并确认弹窗仍指向该引用的原文。 */
export async function checkCitationModal() {
  document.querySelector('[aria-label="查看引用 2 原文"]')?.click()
  const modal = await waitFor(() => {
    const element = document.querySelector('.ant-modal')
    const title = document.querySelector('.ant-modal-title')?.textContent?.trim()
    const excerpt = [...document.querySelectorAll('.citation-excerpt blockquote')].map((node) =>
      node.textContent.trim()
    )
    return element && title && excerpt.length ? { element, title, excerpt } : null
  })
  const bounds = modal.element.getBoundingClientRect()
  return {
    title: modal.title,
    excerpt: modal.excerpt,
    bounds: { left: bounds.left, right: bounds.right, width: window.innerWidth }
  }
}
