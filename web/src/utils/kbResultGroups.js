/** 按知识库文件身份聚合检索片段。 */
export function groupKnowledgeChunks(chunks) {
  const groups = new Map()

  for (const item of chunks) {
    const filename = item?.metadata?.source || '未知来源'
    const kbId = item?.kb_id || ''
    const fileId = item?.file_id || ''
    const key = `${kbId}\u0000${fileId}\u0000${filename}`

    if (!groups.has(key)) {
      const sourceRef = item?.metadata?.source_ref || {}
      groups.set(key, {
        key,
        filename: sourceRef.title || filename,
        kb_id: kbId,
        file_id: fileId,
        source_type: sourceRef.source_type || 'knowledge_base',
        url: sourceRef.url || '',
        author: sourceRef.author || sourceRef.author_name || '',
        chunks: []
      })
    }
    groups.get(key).chunks.push(item)
  }

  return Array.from(groups.values())
}
