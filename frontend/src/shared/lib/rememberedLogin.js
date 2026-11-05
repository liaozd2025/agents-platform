/**
 * 登录页「记住密码」的本地凭据读写。
 *
 * 存储约定：
 * - localStorage key 固定为 REMEMBERED_LOGIN_STORAGE_KEY，值为 base64 编码后的 JSON
 *   `{ loginId, password }`。
 * - base64 只用于避免在 DevTools 里被一眼直读，**它不是加密**，不能作为安全边界；
 *   真正的隔离仍依赖同源策略。本机共用设备场景下不应勾选该选项。
 * - 所有函数都不抛异常：隐私模式禁用 localStorage、浏览器禁用站点数据导致访问 localStorage
 *   抛 SecurityError、内容被手工改坏、历史格式残留等情况都统一降级（读取返回 null），
 *   避免登录页初始化被拖垮。
 */

/** localStorage 中保存登录凭据的键名 */
export const REMEMBERED_LOGIN_STORAGE_KEY = 'remembered_login'

/**
 * UTF-8 字符串转 base64。
 * 不能直接 btoa(str)：遇到非 ASCII 字符（中文账号、含特殊符号的密码）会抛
 * InvalidCharacterError，故先用 TextEncoder 取字节，再逐字节拼成二进制字符串。
 * @param {string} raw 原始字符串
 * @returns {string} base64 字符串
 */
function encodeBase64(raw) {
  const bytes = new TextEncoder().encode(raw)
  let binary = ''
  for (const byte of bytes) {
    binary += String.fromCharCode(byte)
  }
  return btoa(binary)
}

/**
 * base64 转 UTF-8 字符串，是 encodeBase64 的逆操作。
 * @param {string} encoded base64 字符串
 * @returns {string} 解码后的原文
 */
function decodeBase64(encoded) {
  const binary = atob(encoded)
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0))
  return new TextDecoder().decode(bytes)
}

/**
 * 取可用的存储对象。
 * SSR / 单测环境下没有 localStorage 时返回 null；浏览器禁用站点数据、页面处于第三方
 * iframe 等场景下，访问 localStorage 属性本身就会抛 SecurityError，所以这里也必须兜住——
 * 拿不到存储就按「不做记住密码」处理，绝不能让登录页的 onMounted 初始化被打断。
 * @returns {Storage|null} localStorage 或 null
 */
function getStorage() {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch (error) {
    console.warn('[记住密码] 无法访问本地存储，本次跳过记住密码', error?.message)
    return null
  }
}

/**
 * 安全删除指定键。removeItem 与 getItem 一样可能因存储被禁用而抛错，
 * 删除失败只记日志，不向调用方传播（清除属于尽力而为的收尾动作）。
 * @param {Storage} storage 可用的存储对象
 * @param {string} key 待删除的键
 * @returns {void}
 */
function safeRemove(storage, key) {
  try {
    storage.removeItem(key)
  } catch (error) {
    console.warn('[记住密码] 清除本地凭据失败', error?.message)
  }
}

/**
 * 保存登录凭据。仅在登录成功后、且用户勾选了「保持登录」时调用。
 * 账号或密码为空时视为无效输入，不写入，避免把残缺数据覆盖掉已有凭据。
 * @param {string} loginId 登录账号（uid / username / 手机号）
 * @param {string} password 明文密码
 * @returns {boolean} 是否写入成功
 */
export function saveRememberedLogin(loginId, password) {
  const storage = getStorage()
  if (!storage) return false

  const normalizedLoginId = typeof loginId === 'string' ? loginId.trim() : ''
  const normalizedPassword = typeof password === 'string' ? password : ''
  if (!normalizedLoginId || !normalizedPassword) return false

  try {
    const payload = JSON.stringify({
      loginId: normalizedLoginId,
      password: normalizedPassword
    })
    storage.setItem(REMEMBERED_LOGIN_STORAGE_KEY, encodeBase64(payload))
    return true
  } catch (error) {
    // 隐私模式 / 存储配额耗尽 / 站点数据被禁用等：记住密码属可选能力，失败时降级为「不记住」，
    // 由调用方决定是否提示用户；这里只记录原因，不打印数据，避免凭据进入日志
    console.warn('[记住密码] 凭据写入本地存储失败，本次不记住密码', error?.message)
    return false
  }
}

/**
 * 读取已保存的登录凭据。
 * 内容损坏（base64 解码失败、非 JSON、字段缺失或类型不对）时会清掉这条脏数据并返回 null，
 * 避免每次进入登录页都反复解析同一份坏数据。
 * @returns {{loginId: string, password: string}|null} 凭据；无有效凭据时返回 null
 */
export function loadRememberedLogin() {
  const storage = getStorage()
  if (!storage) return null

  try {
    const stored = storage.getItem(REMEMBERED_LOGIN_STORAGE_KEY)
    if (!stored) return null

    const parsed = JSON.parse(decodeBase64(stored))
    const loginId = typeof parsed?.loginId === 'string' ? parsed.loginId : ''
    const password = typeof parsed?.password === 'string' ? parsed.password : ''
    // 账号或密码任一缺失都说明这份凭据不可用（例如历史版本只存了账号），直接丢弃
    if (!loginId || !password) {
      safeRemove(storage, REMEMBERED_LOGIN_STORAGE_KEY)
      return null
    }
    return { loginId, password }
  } catch (error) {
    // 读取本身失败（存储被禁用）或内容损坏（base64 解码失败、非 JSON）都走这里：
    // 返回 null 让登录页保持空表单，并清掉可能存在的脏数据，避免反复解析同一份坏数据
    console.warn('[记住密码] 本地凭据不可用，已忽略并尝试清除', error?.message)
    safeRemove(storage, REMEMBERED_LOGIN_STORAGE_KEY)
    return null
  }
}

/**
 * 清除已保存的登录凭据。
 * 调用时机：用户取消勾选「保持登录」，或未勾选状态下完成登录（清掉上一次的残留）。
 * @returns {void}
 */
export function clearRememberedLogin() {
  const storage = getStorage()
  if (!storage) return
  safeRemove(storage, REMEMBERED_LOGIN_STORAGE_KEY)
}
