import { getPreviewFileExtension } from '@/utils/file_preview'
import { formatRelative, parseToShanghai } from '@/utils/time'

export const formatRelativeTime = (value) => formatRelative(value)

export const formatStandardTime = (value) => {
  const parsed = parseToShanghai(value)
  if (!parsed) return '-'
  return parsed.format('YYYY年MM月DD日 HH:mm:ss')
}

export const getStatusText = (status) => {
  const statusMap = {
    done: '处理完成',
    failed: '处理失败',
    processing: '处理中',
    waiting: '等待处理'
  }
  return statusMap[status] || status
}

export const formatFileSize = (bytes) => {
  if (bytes === 0 || bytes === '0') return '0 B'
  if (!bytes) return '-'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB', 'ZB', 'YB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
}

export const getDisplayFileName = (pathOrName, fallback = '文件') => {
  const value = String(pathOrName || '').trim()
  if (!value) return fallback
  return value.split('/').pop() || value || fallback
}

export const getFileExtensionLabel = (pathOrName) => {
  const extension = getPreviewFileExtension(pathOrName).replace(/^\./, '')
  return extension ? extension.toUpperCase() : ''
}

export const getMimeSubtypeLabel = (mimeType) => {
  const subtype = String(mimeType || '')
    .split('/')
    .pop()
    ?.trim()
  return subtype ? subtype.toUpperCase() : ''
}

export const inferImageMimeTypeFromBase64 = (base64Content) => {
  const head = String(base64Content || '').slice(0, 48)
  if (head.startsWith('iVBORw0KGgo')) return 'image/png'
  if (head.startsWith('/9j/')) return 'image/jpeg'
  if (head.startsWith('R0lGODdh') || head.startsWith('R0lGODlh')) return 'image/gif'
  if (head.startsWith('UklGR')) return 'image/webp'
  if (head.startsWith('Qk')) return 'image/bmp'
  return null
}

export const normalizeAttachmentPreview = (attachment) => {
  const name = getDisplayFileName(
    attachment?.file_name || attachment?.name || attachment?.path,
    '附件'
  )
  const fileId = attachment?.file_id || attachment?.path || name
  const fileType = String(attachment?.file_type || '')
  const sizeLabel = formatFileSize(attachment?.file_size)
  const typeLabel = getFileExtensionLabel(name) || getMimeSubtypeLabel(fileType) || '文件'
  // 右侧面板预览用的 Sandbox runtime 绝对虚拟路径：
  // 优先取 original_path（用户上传的原文件，doc/docx/ppt/pptx 由后端转 PDF 预览），
  // 缺失时回退到 path（附件被解析时指向解析出的 .md）。两者都可直接交给线程 artifact 预览接口。
  // 旧数据若不带路径则留空，调用方据此判定卡片不可点击。
  const path = attachment?.original_path || attachment?.path || ''

  return {
    raw: attachment,
    fileId,
    name,
    path,
    previewUrl: attachment?.original_artifact_url || attachment?.artifact_url || '',
    meta: [typeLabel, sizeLabel === '-' ? '' : sizeLabel].filter(Boolean).join(' · ')
  }
}

export const normalizeAttachmentPreviews = (attachments) => {
  if (!Array.isArray(attachments)) return []
  return attachments.map(normalizeAttachmentPreview).filter((attachment) => attachment.fileId)
}
