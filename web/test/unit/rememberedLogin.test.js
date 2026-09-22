import assert from 'node:assert/strict'
import test, { afterEach, beforeEach } from 'node:test'

import {
  REMEMBERED_LOGIN_STORAGE_KEY,
  clearRememberedLogin,
  loadRememberedLogin,
  saveRememberedLogin
} from '../../src/utils/rememberedLogin.js'

const originalWarn = console.warn
let warnCalls = []

/** 构造一个内存版 localStorage，避免单测依赖真实浏览器存储 */
function createMemoryStorage() {
  const map = new Map()
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: (key) => map.delete(key)
  }
}

/**
 * 落盘格式的期望值取自独立实现（Python base64），硬编码在这里而不是用同一套编码函数现算，
 * 否则同构 oracle 无法发现实现里 UTF-8 处理的偏差。
 */
const ENCODED_CREDENTIALS = 'eyJsb2dpbklkIjoiemhhbmdzYW4iLCJwYXNzd29yZCI6IlBAc3N3MHJk5Lit5paHIn0='
const ENCODED_PLAIN_TEXT = 'cGxhaW4tdGV4dA=='
const ENCODED_LOGIN_ID_ONLY = 'eyJsb2dpbklkIjoiemhhbmdzYW4ifQ=='
const ENCODED_PASSWORD_NOT_STRING = 'eyJsb2dpbklkIjoiemhhbmdzYW4iLCJwYXNzd29yZCI6MTIzfQ=='

beforeEach(() => {
  globalThis.localStorage = createMemoryStorage()
  warnCalls = []
  console.warn = (...args) => warnCalls.push(args)
})

afterEach(() => {
  console.warn = originalWarn
  delete globalThis.localStorage
})

test('保存后能原样读回账号与密码（含中文与非 ASCII 字符）', () => {
  assert.equal(saveRememberedLogin('  zhangsan  ', 'P@ssw0rd中文'), true)

  // 账号被 trim，密码保持原样：密码里的空格可能是有意义的字符
  assert.deepEqual(loadRememberedLogin(), { loginId: 'zhangsan', password: 'P@ssw0rd中文' })
})

test('落盘内容是 base64 编码的 JSON，不是明文直读', () => {
  saveRememberedLogin('zhangsan', 'P@ssw0rd中文')

  const stored = localStorage.getItem(REMEMBERED_LOGIN_STORAGE_KEY)
  assert.equal(stored, ENCODED_CREDENTIALS)
})

test('账号或密码为空时不写入，也不覆盖已有凭据', () => {
  assert.equal(saveRememberedLogin('', 'P@ssw0rd'), false)
  assert.equal(saveRememberedLogin('zhangsan', ''), false)
  assert.equal(localStorage.getItem(REMEMBERED_LOGIN_STORAGE_KEY), null)

  saveRememberedLogin('zhangsan', 'P@ssw0rd')
  assert.equal(saveRememberedLogin('zhangsan', ''), false)
  assert.deepEqual(loadRememberedLogin(), { loginId: 'zhangsan', password: 'P@ssw0rd' })
})

test('清除后读不到凭据', () => {
  saveRememberedLogin('zhangsan', 'P@ssw0rd')
  clearRememberedLogin()

  assert.equal(loadRememberedLogin(), null)
})

test('base64 解码失败时返回 null 并清除脏数据', () => {
  localStorage.setItem(REMEMBERED_LOGIN_STORAGE_KEY, 'not-base64!!')

  assert.equal(loadRememberedLogin(), null)
  assert.equal(localStorage.getItem(REMEMBERED_LOGIN_STORAGE_KEY), null)
  assert.equal(warnCalls.length, 1)
})

test('解码后不是 JSON 时返回 null 并清除脏数据', () => {
  localStorage.setItem(REMEMBERED_LOGIN_STORAGE_KEY, ENCODED_PLAIN_TEXT)

  assert.equal(loadRememberedLogin(), null)
  assert.equal(localStorage.getItem(REMEMBERED_LOGIN_STORAGE_KEY), null)
})

test('字段缺失或类型不对的旧格式凭据会被丢弃', () => {
  // 例如历史版本只存了账号：不可用于自动填充，必须丢弃而不是回填半个表单
  localStorage.setItem(REMEMBERED_LOGIN_STORAGE_KEY, ENCODED_LOGIN_ID_ONLY)
  assert.equal(loadRememberedLogin(), null)
  assert.equal(localStorage.getItem(REMEMBERED_LOGIN_STORAGE_KEY), null)

  localStorage.setItem(REMEMBERED_LOGIN_STORAGE_KEY, ENCODED_PASSWORD_NOT_STRING)
  assert.equal(loadRememberedLogin(), null)
  assert.equal(localStorage.getItem(REMEMBERED_LOGIN_STORAGE_KEY), null)
})

test('无 localStorage 环境（SSR / 单测）下读写都不抛错', () => {
  delete globalThis.localStorage

  assert.equal(loadRememberedLogin(), null)
  assert.equal(saveRememberedLogin('zhangsan', 'P@ssw0rd'), false)
  assert.doesNotThrow(() => clearRememberedLogin())
})

test('写入被拒（配额耗尽 / 隐私模式）时返回 false 并记录原因', () => {
  localStorage.setItem = () => {
    throw new Error('QuotaExceededError')
  }

  assert.equal(saveRememberedLogin('zhangsan', 'P@ssw0rd'), false)
  // 只记录失败原因，不把凭据或完整错误对象写进日志
  assert.equal(warnCalls.length, 1)
  assert.equal(warnCalls[0][1], 'QuotaExceededError')
})

test('存储被禁用（读写抛错）时读取返回 null、清除不抛错', () => {
  const blocked = new Error('SecurityError: storage disabled')
  localStorage.getItem = () => {
    throw blocked
  }
  localStorage.removeItem = () => {
    throw blocked
  }

  assert.equal(loadRememberedLogin(), null)
  assert.doesNotThrow(() => clearRememberedLogin())
  // 记录 3 次：读取失败、读取失败后尝试清除失败、显式清除再失败一次
  assert.equal(warnCalls.length, 3)
})

test('访问 localStorage 属性本身抛错时不向上传播', () => {
  // 浏览器禁用站点数据时，读取 localStorage 属性就会抛 SecurityError；
  // 该路径曾直接暴露给登录页 onMounted，会让其后的健康检查与首启检查全部中断
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    get() {
      throw new Error('SecurityError: access denied')
    }
  })

  assert.equal(loadRememberedLogin(), null)
  assert.equal(saveRememberedLogin('zhangsan', 'P@ssw0rd'), false)
  assert.doesNotThrow(() => clearRememberedLogin())
})
