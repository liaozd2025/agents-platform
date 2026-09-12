import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

/**
 * 接线存在性回归。
 *
 * 这些用例只证明「模块级导航通道确实被嵌入 bridge 注册并清理」「两个来源列表确实经由统一分发」，
 * 不证明点击后的运行时行为——运行时行为由 sourceNavigation.test.js 与 oaEmbedBridge.test.js 覆盖。
 * 缺少本文件时，删除注册或写反清理顺序会让内嵌态静默退化为新标签，而其余用例全部照旧通过。
 */
const source = (path) => readFileSync(new URL(`../../src/${path}`, import.meta.url), 'utf8')

test('嵌入 bridge 注册导航通道，并在停止时清理', () => {
  const text = source('composables/useOAEmbedBridge.js')

  assert.match(
    text,
    /import\s*\{[^}]*setOAEmbedNavigateHandler[^}]*\}\s*from\s*'@\/utils\/oaEmbedSession'/
  )
  // 注册：handler 委托给当前 bridge 的 requestNavigate，未就绪时返回 false 交由调用方降级
  assert.match(
    text,
    /clearNavigateHandler = setOAEmbedNavigateHandler\([\s\S]*?bridge\.requestNavigate\(params\)[\s\S]*?\)/
  )
  // 清理：stopBridge 必须同时清掉授权与导航两个通道
  const stopBridge = text.slice(text.indexOf('const stopBridge ='), text.indexOf('onMounted(startBridge)'))
  assert.match(stopBridge, /clearNavigateHandler\?\.\(\)/)
  assert.match(stopBridge, /clearNavigateHandler = null/)
})

test('两个来源列表都经由统一分发接管左键，并保留原生 href', () => {
  const kbList = source('components/sources/KbResultGroupedList.vue')
  assert.match(kbList, /import\s*\{[^}]*navigateSource[^}]*\}\s*from\s*'@\/utils\/sourceNavigation\.js'/)
  assert.match(kbList, /@click="handleSourceClick\(fileGroup\.url, \$event\)"/)
  // href/target 保留，右键、中键与无障碍访问不受影响
  assert.match(kbList, /:href="fileGroup\.url"[\s\S]{0,120}?target="_blank"/)
  assert.match(kbList, /event\.preventDefault\(\)/)

  const webList = source('components/sources/WebSearchResultList.vue')
  assert.match(webList, /import\s*\{[^}]*navigateSource[^}]*\}\s*from\s*'@\/utils\/sourceNavigation\.js'/)
  assert.match(webList, /@click="handleSourceClick\(result\.url, \$event\)"/)
  assert.match(webList, /:href="result\.url"[\s\S]{0,80}?target="_blank"/)
  assert.match(webList, /event\.preventDefault\(\)/)
})
