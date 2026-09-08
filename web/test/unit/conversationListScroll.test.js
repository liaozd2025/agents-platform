import test from 'node:test'
import assert from 'node:assert/strict'
import { shouldLoadMoreConversations } from '../../src/utils/conversationListScroll.js'

const listAtBottom = { scrollHeight: 1000, scrollTop: 952, clientHeight: 48 }

test('会话列表接近底部且仍有数据时应加载下一页', () => {
  assert.equal(
    shouldLoadMoreConversations({ list: listAtBottom, isLoadingMore: false, hasMoreChats: true }),
    true
  )
})

test('未到底部、加载中或没有更多数据时不应加载', () => {
  assert.equal(
    shouldLoadMoreConversations({
      list: { ...listAtBottom, scrollTop: 900 },
      isLoadingMore: false,
      hasMoreChats: true
    }),
    false
  )
  assert.equal(
    shouldLoadMoreConversations({ list: listAtBottom, isLoadingMore: true, hasMoreChats: true }),
    false
  )
  assert.equal(
    shouldLoadMoreConversations({ list: listAtBottom, isLoadingMore: false, hasMoreChats: false }),
    false
  )
})
