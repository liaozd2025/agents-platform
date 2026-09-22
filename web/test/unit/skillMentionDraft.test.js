import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { prependSkillMentionToNewChatDraft } from '../../src/utils/skill_mention_draft.js'
import { DRAFT_THREAD_ID } from '../../src/utils/thread_draft.js'

/** 内存版草稿存储，避免测试依赖真实 localStorage。 */
const createMemoryStore = (initial = {}) => {
  const data = { ...initial }
  return {
    read: (threadId) => data[threadId] || '',
    write: (threadId, text) => {
      if (text) data[threadId] = text
      else delete data[threadId]
    },
    remove: (threadId) => {
      delete data[threadId]
    },
    snapshot: () => ({ ...data })
  }
}

const source = (path) => readFileSync(new URL(`../../src/${path}`, import.meta.url), 'utf8')

test('空草稿写入技能提及后直接保留提及与尾随空格', () => {
  const store = createMemoryStore()
  const text = prependSkillMentionToNewChatDraft('humanizer-zh', store)

  assert.equal(text, '@skill:humanizer-zh ')
  assert.equal(store.read(DRAFT_THREAD_ID), '@skill:humanizer-zh ')
})

test('已有问话时技能提及排到最前面，问话顺延到技能之后', () => {
  const store = createMemoryStore({ [DRAFT_THREAD_ID]: '帮我写一段周报' })
  const text = prependSkillMentionToNewChatDraft('translator', store)

  assert.equal(text, '@skill:translator 帮我写一段周报 ')
  // 顺序必须固定为「@技能 + 用户问话」
  assert.ok(text.indexOf('@skill:translator') < text.indexOf('帮我写一段周报'))
})

test('已有草稿首尾空白不会被带进新草稿', () => {
  const store = createMemoryStore({ [DRAFT_THREAD_ID]: '  帮我翻译  ' })
  const text = prependSkillMentionToNewChatDraft('translator', store)

  assert.equal(text, '@skill:translator 帮我翻译 ')
})

test('重复点击同一技能不会重复插入', () => {
  const store = createMemoryStore({ [DRAFT_THREAD_ID]: '@skill:translator ' })
  const text = prependSkillMentionToNewChatDraft('translator', store)

  assert.equal(text, '@skill:translator ')
})

test('草稿里已有其他技能时，新技能插到最前面且不丢原有技能', () => {
  const store = createMemoryStore({ [DRAFT_THREAD_ID]: '@skill:translator 帮我翻译' })
  const text = prependSkillMentionToNewChatDraft('pptx-manipulation', store)

  assert.equal(text, '@skill:pptx-manipulation @skill:translator 帮我翻译 ')
})

test('含空格的技能 slug 使用带引号的提及格式', () => {
  const store = createMemoryStore()
  const text = prependSkillMentionToNewChatDraft('my skill', store)

  assert.equal(text, '@skill:"my skill" ')
})

test('空 slug 不写入草稿', () => {
  const store = createMemoryStore({ [DRAFT_THREAD_ID]: '已有内容' })
  const text = prependSkillMentionToNewChatDraft('  ', store)

  assert.equal(text, '已有内容')
  assert.deepEqual(store.snapshot(), { [DRAFT_THREAD_ID]: '已有内容' })
})

test('草稿写入后由对话页把光标定位到末尾，避免打字跑到 @技能 前面', () => {
  const input = source('components/MessageInputComponent.vue')
  // 输入框提供「聚焦并落到末尾」的方法，并对外暴露
  assert.match(input, /const focusInputEnd = \(\) => \{/)
  assert.match(input, /range\.selectNodeContents\(inputRef\.value\)/)
  assert.match(input, /range\.collapse\(false\)/)
  assert.match(input, /focusEnd: focusInputEnd/)

  const inputArea = source('components/AgentInputArea.vue')
  assert.match(inputArea, /focusEnd: \(\) => inputRef\.value\?\.focusEnd\(\)/)

  const chat = source('components/AgentChatComponent.vue')
  // 同步草稿后必须紧接着把光标移到末尾
  assert.match(
    chat,
    /userInput\.value = latestDraft\s*\/\/[^\n]*\n\s*nextTick\(\(\) => \{\s*agentInputAreaRef\.value\?\.focusEnd\(\)\s*\}\)/
  )
  // 首次进入对话页（草稿在初始化阶段读入）同样要定位末尾
  assert.match(
    chat,
    /if \(!currentChatId\.value && userInput\.value\) \{\s*agentInputAreaRef\.value\?\.focusEnd\(\)\s*\}/
  )

  const list = source('components/extensions/SkillCardList.vue')
  assert.match(list, /prependSkillMentionToNewChatDraft\(skill\.slug\)/)
  assert.doesNotMatch(list, /appendSkillMentionToNewChatDraft/)
})
