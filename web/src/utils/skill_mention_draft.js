/**
 * 「立即使用技能」：把技能提及写入新建对话的输入草稿。
 *
 * 对话页的输入框在挂载/激活时会读取该草稿（thread_draft 的 DRAFT_THREAD_ID），
 * 因此技能广场只需写入草稿并跳转即可，不需要跨页面传递组件引用。
 */

import { formatMentionToken } from './mention_token.js'
import { createThreadDraftStore, DRAFT_THREAD_ID } from './thread_draft.js'

/**
 * 把技能提及放到新建对话草稿的最前面，用户已有的问话内容顺延到技能之后。
 *
 * 顺序固定为「@技能 + 用户问话」，这样用户接着打字时问话始终在技能后面；
 * 已有草稿不会被覆盖，重复点击同一技能不会重复插入。
 *
 * @param {string} slug 技能 slug，与输入框 @ 技能时使用的标识一致
 * @param {ReturnType<typeof createThreadDraftStore>} [store] 可注入的草稿存储
 * @returns {string} 写入后的草稿文本
 */
export const prependSkillMentionToNewChatDraft = (slug, store = createThreadDraftStore()) => {
  const normalizedSlug = String(slug || '').trim()
  if (!normalizedSlug) return store.read(DRAFT_THREAD_ID)

  const token = formatMentionToken('skill', normalizedSlug)
  const existing = String(store.read(DRAFT_THREAD_ID) || '')
  if (existing.includes(token)) return existing

  const rest = existing.trim()
  const next = rest ? `${token} ${rest} ` : `${token} `
  store.write(DRAFT_THREAD_ID, next)
  return next
}
