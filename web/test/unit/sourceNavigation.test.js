import assert from 'node:assert/strict'
import test from 'node:test'

import {
  isOAInternalUrl,
  navigateSource,
  parseOANavigateParams
} from '../../src/utils/sourceNavigation.js'
import { requestOAEmbedNavigate, setOAEmbedNavigateHandler } from '../../src/utils/oaEmbedSession.js'

/** PR68 后端产出的正式 OA 文章地址（新闻详细，page_type=3）。 */
const PR68_NEWS_URL =
  'https://hnjiudian.cn/web/index.html#/corporate-culture/view-page/3' +
  '?title=%E6%96%B0%E9%97%BB%E8%AF%A6%E7%BB%86-2441705&taskID=2441705&ecType=3'

/** 早期历史数据：URL 中没有 ecType，只有路径段能提供文章类型。 */
const LEGACY_NEWS_URL =
  'https://hnjiudian.cn/web/index.html#/corporate-culture/view-page/3' +
  '?title=%E6%96%B0%E9%97%BB%E8%AF%A6%E7%BB%86-2441705&taskID=2441705'

test('OA 内部判定只接受 hnjiudian.cn 及其子域', () => {
  assert.equal(isOAInternalUrl('https://hnjiudian.cn/web/index.html#/a'), true)
  assert.equal(isOAInternalUrl('http://oa.hnjiudian.cn/article?id=1'), true)
  // 大小写与末尾点由 URL 规范化处理，仍属内部
  assert.equal(isOAInternalUrl('https://HNJIUDIAN.CN/web/index.html#/a'), true)
  assert.equal(isOAInternalUrl('https://hnjiudian.cn./web/index.html#/a'), true)
  // 带端口与带凭据不影响 hostname 判定
  assert.equal(isOAInternalUrl('https://hnjiudian.cn:8443/web/index.html#/a'), true)
  assert.equal(isOAInternalUrl('https://oa-user@hnjiudian.cn/web/index.html#/a'), true)
  assert.equal(isOAInternalUrl('https://hnjiudian.cn.evil.example.com/a'), false)
  assert.equal(isOAInternalUrl('https://not-hnjiudian.cn/a'), false)
  assert.equal(isOAInternalUrl('https://www.example.com/a'), false)
  // 凭据里的域名不能冒充目标站点
  assert.equal(isOAInternalUrl('https://hnjiudian.cn@evil.example.com/a'), false)
  // 相对路径无 hostname，按外部处理
  assert.equal(isOAInternalUrl('/web/index.html#/corporate-culture/view-page/3'), false)
  assert.equal(isOAInternalUrl('javascript:alert(1)'), false)
  assert.equal(isOAInternalUrl(''), false)
})

test('解析 PR68 正式 URL 得到 taskId 与 ecType', () => {
  assert.deepEqual(parseOANavigateParams(PR68_NEWS_URL), { taskId: '2441705', ecType: '3' })
})

test('ecType 缺失时从 view-page 路径段兜底，兼容历史数据', () => {
  assert.deepEqual(parseOANavigateParams(LEGACY_NEWS_URL), { taskId: '2441705', ecType: '3' })
})

test('好文共享地址解析出 ecType=1', () => {
  const url =
    'https://hnjiudian.cn/web/index.html#/corporate-culture/view-page/1' +
    '?title=%E8%AE%B2%E4%B9%9D%E5%85%B8%E6%95%85%E4%BA%8B&taskID=3104861&ecType=1'
  assert.deepEqual(parseOANavigateParams(url), { taskId: '3104861', ecType: '1' })
})

test('常规查询串形式同样可解析，并兼容 task_id/type 命名', () => {
  assert.deepEqual(parseOANavigateParams('https://hnjiudian.cn/view?task_id=99&type=1'), {
    taskId: '99',
    ecType: '1'
  })
  assert.deepEqual(parseOANavigateParams('https://hnjiudian.cn/view?taskId=100&ecType=3'), {
    taskId: '100',
    ecType: '3'
  })
})

test('缺少 taskId、ecType 或地址不可解析时返回 null', () => {
  assert.equal(
    parseOANavigateParams('https://hnjiudian.cn/web/index.html#/view-page/3?ecType=3'),
    null
  )
  assert.equal(parseOANavigateParams('https://hnjiudian.cn/web?taskID=1'), null)
  assert.equal(parseOANavigateParams('/web/index.html#/corporate-culture/view-page/3'), null)
  assert.equal(parseOANavigateParams(''), null)
  assert.equal(parseOANavigateParams(null), null)
})

test('内嵌态下内部链接走父页面跳转，不打开新标签', () => {
  const navigations = []
  const clear = setOAEmbedNavigateHandler((params) => {
    navigations.push(params)
    return true
  })
  const opened = []
  const result = navigateSource({
    sourceType: 'knowledge_base',
    url: PR68_NEWS_URL,
    openWindow: (...args) => opened.push(args)
  })
  clear()

  assert.equal(result, 'internal-embed')
  assert.deepEqual(navigations, [{ taskId: '2441705', ecType: '3' }])
  assert.deepEqual(opened, [])
})

test('外部链接一律新标签打开，且不经过嵌入通道', () => {
  let embedRequests = 0
  const clear = setOAEmbedNavigateHandler(() => {
    embedRequests += 1
    return true
  })
  const opened = []
  const result = navigateSource({
    sourceType: 'web_search',
    url: 'https://www.example.com/news/1',
    openWindow: (...args) => opened.push(args)
  })
  clear()

  assert.equal(result, 'external')
  assert.equal(embedRequests, 0)
  assert.deepEqual(opened, [['https://www.example.com/news/1', '_blank', 'noopener,noreferrer']])
})

test('非内嵌态或参数不可解析时降级为新标签，不丢跳转能力', () => {
  const opened = []
  const openWindow = (...args) => opened.push(args)

  // 未注册嵌入处理器（独立站场景）
  assert.equal(navigateSource({ url: PR68_NEWS_URL, openWindow }), 'external')
  // 内嵌但链接缺少 ecType 与路径段
  const clear = setOAEmbedNavigateHandler(() => true)
  assert.equal(
    navigateSource({ url: 'https://hnjiudian.cn/web/index.html#/view?taskID=1', openWindow }),
    'external'
  )
  clear()

  assert.deepEqual(opened, [
    [PR68_NEWS_URL, '_blank', 'noopener,noreferrer'],
    ['https://hnjiudian.cn/web/index.html#/view?taskID=1', '_blank', 'noopener,noreferrer']
  ])
  assert.equal(requestOAEmbedNavigate({ taskId: '1', ecType: '3' }), false)
})

test('空地址不触发任何跳转', () => {
  const opened = []
  assert.equal(navigateSource({ url: '  ', openWindow: (...args) => opened.push(args) }), 'none')
  assert.deepEqual(opened, [])
})

test('新标签被浏览器拦截时仍返回 external 并留下告警', () => {
  const warnings = []
  const originalWarn = console.warn
  console.warn = (...args) => warnings.push(args.join(' '))
  let result
  try {
    // 返回 null 模拟弹窗拦截
    result = navigateSource({
      sourceType: 'web_search',
      url: 'https://www.example.com/news/1',
      openWindow: () => null
    })
  } finally {
    console.warn = originalWarn
  }

  assert.equal(result, 'external')
  assert.equal(
    warnings.some((line) => line.includes('新标签被拦截')),
    true
  )
})
