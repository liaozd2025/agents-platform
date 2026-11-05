/**
 * 检索来源列表的点击跳转分发。
 *
 * 目标：区分「OA 内部链接」与「外部链接」
 * - 内部链接：独立站打开时新标签跳 OA 原文；iframe 内嵌时改为通知父页面路由跳转，
 *   避免 iframe 自身被顶掉（AI 会话页面保留）。
 * - 外部链接：一律新标签打开。
 *
 * 协议与 jd-ai-h5 保持一致：父页面接收 `new-navigate`，载荷为 { taskId, ecType }。
 */

import { requestOAEmbedNavigate } from './oaEmbedSession.js'

/** OA 内部站点的根域名，命中该域名或其子域即视为内部链接。 */
const OA_INTERNAL_HOSTNAMES = Object.freeze(['hnjiudian.cn'])

/** OA 文章详情页的 hash 路径段，形如 `#/corporate-culture/view-page/3`。 */
const OA_VIEW_PAGE_SEGMENT_PATTERN = /view-page\/(\d+)/i

/** 查询串中任务号的候选键名，覆盖后端历史与现状的多种写法。 */
const TASK_ID_KEYS = Object.freeze(['taskId', 'taskID', 'task_id'])

/** 查询串中文章类型（ecType）的候选键名。 */
const EC_TYPE_KEYS = Object.freeze(['ecType', 'type'])

/** 判定 URL 是否指向 OA 内部站点；非绝对 URL 或解析失败一律按外部处理。 */
export function isOAInternalUrl(url) {
  const hostname = resolveHostname(url)
  if (!hostname) return false
  return OA_INTERNAL_HOSTNAMES.some(
    (base) => hostname === base || hostname.endsWith(`.${base}`)
  )
}

/**
 * 解析 OA 内部跳转所需的 { taskId, ecType }。
 *
 * 参数来源有两处，需按优先级合并（后端产出的正式格式把两者都放在 hash 内的查询串）：
 * 1. hash 内的查询串，如 `#/corporate-culture/view-page/3?title=..&taskID=..`
 * 2. 常规 search，如 `?taskId=1&ecType=3`
 *
 * ecType 缺失时再从 `view-page/{n}` 路径段兜底，用于兼容早期未写入 ecType 的历史数据。
 * 任一参数最终无法取得时返回 null，由调用方降级为新标签打开。
 */
export function parseOANavigateParams(url) {
  const text = String(url || '').trim()
  if (!text) return null

  let parsed
  try {
    parsed = new URL(text)
  } catch {
    // 相对路径等无法解析的地址不是有效的 OA 内部跳转目标。
    return null
  }

  const hashQuery = extractHashQuery(parsed.hash)
  const sources = [hashQuery, parsed.search]
  const taskId = pickParam(sources, TASK_ID_KEYS)
  const ecType = pickParam(sources, EC_TYPE_KEYS) || extractPageTypeFromPath(parsed)

  if (!taskId || !ecType) return null
  return { taskId, ecType }
}

/**
 * 检索来源点击的唯一分发入口。
 *
 * @param {object} params
 * @param {string} params.url 目标地址
 * @param {string} [params.sourceType] 来源类型，仅用于日志区分
 * @param {Function} [params.openWindow] 新标签打开实现，便于测试注入
 * @returns {'none'|'internal-embed'|'external'} 实际执行的分支，便于调用方与测试断言
 */
export function navigateSource({ url, sourceType = '', openWindow } = {}) {
  const target = String(url || '').trim()
  if (!target) return 'none'

  if (isOAInternalUrl(target)) {
    const params = parseOANavigateParams(target)
    if (params) {
      // 内嵌态由父页面完成路由跳转；未内嵌（无活动桥）时返回 false，继续走下面的新标签兜底。
      if (requestOAEmbedNavigate(params)) {
        console.info(`[来源跳转] 已在 OA 内嵌态请求父页面跳转（${sourceType || 'unknown'}）`)
        return 'internal-embed'
      }
    } else {
      console.warn(`[来源跳转] OA 内部链接缺少 taskId/ecType，降级为新标签打开（${sourceType || 'unknown'}）`)
    }
  }

  const open = resolveOpenWindow(openWindow)
  if (!open) {
    console.warn('[来源跳转] 当前环境不支持打开新窗口，已跳过跳转')
    return 'none'
  }
  // open 返回 null 表示新标签被浏览器拦截；调用方已 preventDefault，这里补一条可观察信号。
  if (!open(target, '_blank', 'noopener,noreferrer')) {
    console.warn(`[来源跳转] 新标签被拦截，来源未能打开（${sourceType || 'unknown'}）`)
  }
  return 'external'
}

/** 从 URL 中安全取出小写化的 hostname，失败返回空串。 */
function resolveHostname(url) {
  try {
    return new URL(String(url || '').trim()).hostname.toLowerCase().replace(/\.$/, '')
  } catch {
    return ''
  }
}

/** 抽取 hash 内 `?` 之后的查询串，保持原样交由 URLSearchParams 解析。 */
function extractHashQuery(hash) {
  const index = String(hash || '').indexOf('?')
  return index === -1 ? '' : hash.slice(index + 1)
}

/** 按候选键名依次在多个查询串中查找首个非空值。 */
function pickParam(sources, keys) {
  for (const source of sources) {
    if (!source) continue
    let params
    try {
      params = new URLSearchParams(source)
    } catch {
      continue
    }
    for (const key of keys) {
      const value = String(params.get(key) ?? '').trim()
      if (value) return value
    }
  }
  return ''
}

/** 从 `view-page/{n}` 路径段兜底取文章类型，用于兼容历史缺参数据。 */
function extractPageTypeFromPath(parsed) {
  const matched =
    OA_VIEW_PAGE_SEGMENT_PATTERN.exec(parsed.hash || '') ||
    OA_VIEW_PAGE_SEGMENT_PATTERN.exec(parsed.pathname || '')
  return matched ? matched[1] : ''
}

/** 解析新标签打开能力；浏览器外的测试环境可通过参数注入。 */
function resolveOpenWindow(openWindow) {
  if (typeof openWindow === 'function') return openWindow
  if (typeof window !== 'undefined' && typeof window.open === 'function') {
    return window.open.bind(window)
  }
  return null
}
