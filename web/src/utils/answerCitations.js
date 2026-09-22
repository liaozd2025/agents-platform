import { escapeHtml } from './html.js'

/** 只允许引用打开绝对 HTTP(S) 地址。 */
export function safeCitationUrl(value) {
  if (typeof value !== 'string' || !/^https?:\/\//i.test(value.trim())) return ''
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password
      ? value.trim()
      : ''
  } catch {
    return ''
  }
}

/** 在 Markdown 行内语法中编号，代码块和行内代码自然保持原样。 */
export function markdownItCitations(md) {
  md.inline.ruler.before('html_inline', 'answer_citation', (state, silent) => {
    const match = /^<cite\s+([^>]+)>\s*\d+\s*<\/cite>/i.exec(state.src.slice(state.pos))
    if (!match) return false
    const sourceAttr = /(?:^|\s)source\s*=\s*(?:"([^"]+)"|'([^']+)')/i.exec(match[1])
    if (!sourceAttr) return false
    if (!silent) {
      const source = md.utils.unescapeAll(sourceAttr[1] || sourceAttr[2])
      const numbers = (state.env.answerCitationNumbers ||= new Map())
      if (!numbers.has(source)) numbers.set(source, numbers.size + 1)
      const token = state.push('answer_citation', '', 0)
      token.meta = { source, number: numbers.get(source) }
    }
    state.pos += match[0].length
    return true
  })
  md.renderer.rules.answer_citation = (tokens, index) => {
    const { source, number } = tokens[index].meta
    return `<button type="button" class="citation-ref" data-citation-source="${escapeHtml(source)}" aria-label="查看引用 ${number} 原文" aria-haspopup="dialog">${number}</button>`
  }
}

/** 合并同一稳定来源的真实片段，不以标题或模型的局部编号关联。 */
export function mergeCitationSources(sources) {
  const merged = new Map()
  for (const item of Array.isArray(sources) ? sources : []) {
    if (!item || typeof item.source !== 'string' || !item.source) continue
    let source = merged.get(item.source)
    if (!source) {
      source = {
        source: item.source,
        source_type: item.source_type,
        title: typeof item.title === 'string' ? item.title : '',
        url: safeCitationUrl(item.url) || safeCitationUrl(item.source),
        kb_id: typeof item.kb_id === 'string' ? item.kb_id : '',
        file_id: typeof item.file_id === 'string' ? item.file_id : '',
        excerpts: []
      }
      merged.set(item.source, source)
    }
    for (const excerpt of Array.isArray(item.excerpts) ? item.excerpts : []) {
      if (typeof excerpt?.text !== 'string' || !excerpt.text.trim()) continue
      if (!source.excerpts.some((existing) => existing.text === excerpt.text)) {
        source.excerpts.push(excerpt)
      }
    }
  }
  return [...merged.values()]
}

/** 未绑定文件不猜测来源；只有安全 URL 能提供无片段入口。 */
export function resolveAnswerCitation(source, sources) {
  const match = mergeCitationSources(sources).find((item) => item.source === source)
  return match || { source, title: '', url: safeCitationUrl(source), excerpts: [] }
}

/** 解析当前工具已有结果，失败时不从正文猜测证据。 */
function parseResult(content) {
  if (content && typeof content === 'object') return content
  try {
    return JSON.parse(content)
  } catch {
    return null
  }
}

/** 从同步和异步子任务结果读取后端绑定的证据。 */
export function getToolCitationSources(toolCall) {
  const response = toolCall?.tool_call_result
  const parsed = parseResult(response?.content ?? toolCall?.result)
  return mergeCitationSources(
    [
      response?.artifact?.citation_sources || response?.extra_metadata?.artifact?.citation_sources,
      toolCall?.subagent_run?.citation_sources,
      parsed?.citation_sources || parsed?.result?.citation_sources
    ].flatMap((items) => (Array.isArray(items) ? items : []))
  )
}

/** 旧知识片段只允许稳定文件身份或精确 URL 绑定。 */
function legacyKnowledgeSources(chunks, kbId = '') {
  return (Array.isArray(chunks) ? chunks : []).flatMap((chunk) => {
    const metadata = chunk?.metadata || {}
    const ref = metadata.source_ref || {}
    const kb = chunk?.kb_id || kbId
    const file = chunk?.file_id || metadata.file_id
    const url = safeCitationUrl(ref.url)
    const source = kb && file ? `kb://${kb}/${file}` : url
    if (!source) return []
    const item = {
      source,
      source_type: 'file',
      title: ref.title || metadata.source || '',
      kb_id: kb,
      file_id: file,
      url,
      excerpts:
        typeof chunk.content === 'string'
          ? [
              {
                text: chunk.content,
                chunk_id: metadata.chunk_id,
                start_line: metadata.start_line,
                end_line: metadata.end_line
              }
            ]
          : []
    }
    return url && url !== source ? [item, { ...item, source: url }] : [item]
  })
}

/** 只读取当前消息的证据，历史记录不借用相邻消息或重写正文。 */
export function getMessageCitationSources(message) {
  const persisted = message?.extra_metadata?.citation_sources
  if (Array.isArray(persisted)) return mergeCitationSources(persisted)
  const sources = legacyKnowledgeSources(message?.extra_metadata?.knowledge_sources)
  for (const call of message?.tool_calls || []) {
    sources.push(...getToolCitationSources(call))
    const name = call.name || call.function?.name || ''
    const parsed = parseResult(call.tool_call_result?.content ?? call.result)
    const args = parseResult(call.args ?? call.function?.arguments) || {}
    if (name === 'query_kb') {
      sources.push(
        ...legacyKnowledgeSources(
          Array.isArray(parsed) ? parsed : parsed?.results || parsed?.data?.chunks,
          args.kb_id
        )
      )
    }
    if (['open_kb_document', 'find_kb_document'].includes(name) && parsed && !parsed.error) {
      const windows = Array.isArray(parsed.windows) ? parsed.windows : [parsed]
      sources.push(
        ...legacyKnowledgeSources(
          windows.map((window) => ({
            content: window.content,
            kb_id: parsed.kb_id || args.kb_id,
            file_id: parsed.file_id || args.file_id,
            metadata: {
              source: parsed.title || parsed.file_name,
              source_ref: parsed.source_ref,
              start_line: window.start_line,
              end_line: window.end_line
            }
          }))
        )
      )
    }
    if (/^(?:web_search|tavily_search|doubao_search)$/.test(name)) {
      for (const item of Array.isArray(parsed?.results) ? parsed.results : []) {
        if (!safeCitationUrl(item?.url)) continue
        sources.push({
          source: item.url,
          source_type: 'url',
          title: item.title,
          url: item.url,
          excerpts: typeof item.raw_content === 'string' ? [{ text: item.raw_content }] : []
        })
      }
    }
  }
  return mergeCitationSources(sources)
}
