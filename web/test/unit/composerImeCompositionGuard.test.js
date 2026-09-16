import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

/**
 * 回归目标：中文输入法组合态（预编辑）下按回车是"确认上屏"，不能触发消息发送。
 *
 * macOS 的中文输入法会把这次回车以 key="Enter"、isComposing=true 上报，只判断 e.key
 * 的发送逻辑会把输入英文/候选词的回车当成发送；Windows 的输入法通常只上报
 * key="Process"/keyCode=229，因此掩盖了该缺陷（平台差异是缺陷被隐藏的原因，不是缺陷本身）。
 *
 * 本文件是源码断言：仓库没有 jsdom 与 @vue/test-utils，无法在单测内派发真实的组合态事件，
 * 因此只固定"守卫存在且位于发送分支之前"，运行时行为需在 macOS 上手工验证。
 */
const source = (path) => readFileSync(new URL(`../../src/${path}`, import.meta.url), 'utf8')

// 提取箭头函数体，确保断言落在目标函数内部，而不是文件里任意出现的同名变量上
const extractFunctionBody = (text, functionName) => {
  const match = text.match(new RegExp(`const ${functionName} = [^]*?\\n\\}`))
  assert.ok(match, `未找到函数 ${functionName}`)
  return match[0]
}

test('发送判定在组合态内提前返回，且早于回车发送分支', () => {
  const text = source('components/AgentInputArea.vue')
  const body = extractFunctionBody(text, 'handleKeyDown')

  const guardIndex = body.indexOf('e.isComposing')
  const sendIndex = body.indexOf("if (e.key === 'Enter' && !e.shiftKey)")

  assert.notEqual(guardIndex, -1, 'handleKeyDown 必须判断事件自带的组合态标记')
  assert.notEqual(sendIndex, -1, 'handleKeyDown 必须保留回车发送分支')
  assert.ok(guardIndex < sendIndex, '组合态守卫必须位于回车发送分支之前，否则仍会误发')
  assert.match(body, /keyCode === 229/, '必须兼容只上报 Process 键的输入法（keyCode 229）')
})

test('输入框键盘处理在组合态内整体早退，覆盖发送与弹窗导航', () => {
  const text = source('components/MessageInputComponent.vue')
  const body = extractFunctionBody(text, 'handleKeyPress')

  const guardIndex = body.indexOf('isImeCompositionKey(e)')
  const mentionIndex = body.indexOf('mentionPopupVisible.value')
  const emitIndex = body.indexOf("emit('keydown', e)")

  assert.notEqual(guardIndex, -1, 'handleKeyPress 必须调用组合态判据')
  assert.ok(guardIndex < mentionIndex, '组合态早退必须早于 @ 提及弹窗回车导航')
  assert.ok(guardIndex < emitIndex, '组合态早退必须早于把事件透传给父组件的发送判定')
})

test('组合态判据覆盖事件字段、Process 键与组合结束后时间窗', () => {
  const text = source('components/MessageInputComponent.vue')
  const body = extractFunctionBody(text, 'isImeCompositionKey')

  assert.match(body, /isComposing\.value/, '需要组件自身维护的组合态标记')
  assert.match(body, /e\.isComposing/, '需要事件自带的组合态标记')
  assert.match(body, /keyCode === 229/, '需要兼容只上报 Process 键的输入法')
  assert.match(body, /lastCompositionEndAt/, '需要 compositionend 之后的时间窗兜底（Safari 时序）')

  const endBody = extractFunctionBody(text, 'handleCompositionEnd')
  assert.match(endBody, /lastCompositionEndAt = Date\.now\(\)/, '组合结束必须记录时刻供时间窗使用')
})
