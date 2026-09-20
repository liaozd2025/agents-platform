import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'

// 消息气泡里的文件附件必须可点击并交给右侧面板预览（图片走内联大图，不在本约束内）。
// 这里用源码断言固定这条装配链：file_utils 提供 runtime 路径 → 卡片点击 emit → 聊天组件接住并打开面板。
const readSource = (relativePath) =>
  readFileSync(new URL(`../../src/${relativePath}`, import.meta.url), 'utf8')

test('附件预览路径优先取 original_path 并回退 path', () => {
  const source = readSource('utils/file_utils.js')
  const start = source.indexOf('export const normalizeAttachmentPreview')
  const end = source.indexOf('export const normalizeAttachmentPreviews', start)
  const normalizer = source.slice(start, end)

  assert.notEqual(start, -1)
  assert.notEqual(end, -1)
  assert.match(normalizer, /const path = attachment\?\.original_path \|\| attachment\?\.path \|\| ''/)
  assert.match(normalizer, /^\s*path,$/m)
})

test('用户消息附件卡片点击后 emit open-attachment 事件', () => {
  const source = readSource('components/AgentMessageComponent.vue')

  // 卡片必须渲染为可交互元素并绑定点击，无路径时禁用点击
  assert.match(source, /:disabled="!attachment\.path"/)
  assert.match(source, /@click="openAttachmentPreview\(attachment\)"/)
  // 事件载荷只带面板需要的 path 与展示名
  assert.match(source, /emit\('open-attachment', \{ path: attachment\.path, name: attachment\.name \}\)/)
  // 无路径（旧数据）时不冒泡
  assert.match(source, /const openAttachmentPreview = \(attachment\) => \{\s*if \(!attachment\?\.path\) return/)
})

test('聊天组件监听附件点击并复用 artifact 预览打开右侧面板', () => {
  const source = readSource('components/AgentChatComponent.vue')

  assert.match(source, /@open-attachment="openAttachmentPreview"/)
  assert.match(source, /const openAttachmentPreview = \(attachment\) => \{\s*if \(!attachment\?\.path\) return\s*openArtifactPreview\(/)
})
