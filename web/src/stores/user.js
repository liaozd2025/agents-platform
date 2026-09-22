import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { authApi } from '@/apis/auth_api'
import { useAgentStore } from './agent'

export const useUserStore = defineStore('user', () => {
  // 状态
  // 优先恢复跨会话令牌；未勾选“保持登录”时只从当前会话恢复。
  const sessionTokenStorage = typeof sessionStorage === 'undefined' ? null : sessionStorage

  /**
   * 首屏恢复令牌：localStorage（保持登录）优先，缺失时才退回 sessionStorage（仅本次会话）。
   * 取用后清理另一侧残留，避免上一次登录遗留的旧令牌被继续当成有效登录态。
   * @returns {string} 可用的 JWT；两处都没有时返回空串
   */
  function restorePersistedToken() {
    const persistentToken = localStorage.getItem('user_token')
    if (persistentToken) {
      // 存在跨会话令牌即以它为准，同时清掉会话令牌，防止同一浏览器两个存储分叉
      sessionTokenStorage?.removeItem('user_token')
      return persistentToken
    }
    return sessionTokenStorage?.getItem('user_token') || ''
  }

  const token = ref(restorePersistedToken())
  const userId = ref(null)
  const username = ref('')
  // displayName 是界面展示姓名，username 仍仅作为登录账号使用。
  const displayName = ref('')
  const uid = ref('')
  const phoneNumber = ref('')
  const avatar = ref('')
  const userRoles = ref([])
  const effectivePermissions = ref([])
  const departmentId = ref(null)
  const departmentName = ref('')
  let authSessionController = new AbortController()

  // 计算属性
  const isLoggedIn = computed(() => !!token.value)
  const hasPermission = (permissionKey) => effectivePermissions.value.includes(permissionKey)

  // 动作
  /**
   * 按“保持登录 30 天”勾选状态持久化令牌，供所有登录入口复用。
   * rememberLogin 为 true（默认值，与后端 30 天有效期一致）：写 localStorage，关闭浏览器后仍保持登录；
   * rememberLogin 为 false：只写 sessionStorage，关闭标签页即失效。
   * 写入前先清空两侧，保证两种存储互斥，避免取消勾选后仍被旧令牌自动登录。
   * @param {string} accessToken 后端下发的访问令牌
   * @param {boolean} rememberLogin 是否跨浏览器会话保留登录态
   */
  function persistToken(accessToken, rememberLogin = true) {
    // 无 sessionStorage（SSR / 单测环境）时降级到 localStorage，保证初始化不失败
    const tokenStorage = rememberLogin || !sessionTokenStorage ? localStorage : sessionTokenStorage
    localStorage.removeItem('user_token')
    sessionTokenStorage?.removeItem('user_token')
    tokenStorage.setItem('user_token', accessToken)
  }

  function applySession(data, rememberLogin = true) {
    token.value = data.access_token
    userId.value = data.user_id
    username.value = data.username
    displayName.value = data.display_name || ''
    uid.value = data.uid
    phoneNumber.value = data.phone_number || ''
    avatar.value = data.avatar || ''
    userRoles.value = data.roles || []
    effectivePermissions.value = data.effective_permissions || []
    departmentId.value = data.department_id || null
    departmentName.value = data.department_name || ''
    persistToken(data.access_token, rememberLogin)
  }

  async function login(credentials) {
    try {
      const data = await authApi.login(credentials)
      applySession(data, credentials.rememberLogin)
      await getCurrentUser()
      return true
    } catch (error) {
      if (token.value) logout()
      console.error('登录错误:', error)
      throw error
    }
  }

  function logout() {
    // 认证会话控制器的 abort 会取消所有正在进行的业务请求；记录调用堆栈用于定位误登出来源。
    console.warn('[认证诊断] userStore.logout，正在取消当前会话请求', {
      path: typeof window !== 'undefined' ? window.location.pathname : '',
      userId: userId.value,
      hasToken: Boolean(token.value),
      stack: new Error().stack
    })
    authSessionController.abort()
    authSessionController = new AbortController()

    // 清除状态
    token.value = ''
    userId.value = null
    username.value = ''
    displayName.value = ''
    uid.value = ''
    phoneNumber.value = ''
    avatar.value = ''
    userRoles.value = []
    effectivePermissions.value = []
    departmentId.value = null
    departmentName.value = ''

    // 清除 agentStore 状态，确保重新登录时能正确加载数据
    const agentStore = useAgentStore()
    agentStore.reset()

    // 只清除 token
    localStorage.removeItem('user_token')
    sessionTokenStorage?.removeItem('user_token')
  }

  async function initialize(admin) {
    try {
      const data = await authApi.initialize(admin)
      applySession(data)
      await getCurrentUser()
      return true
    } catch (error) {
      if (token.value) logout()
      console.error('初始化管理员错误:', error)
      throw error
    }
  }

  async function checkFirstRun() {
    try {
      const data = await authApi.checkFirstRun()
      return data.first_run
    } catch (error) {
      console.error('检查首次运行状态错误:', error)
      return false
    }
  }

  // 用于API请求的授权头
  function getAuthHeaders() {
    return {
      Authorization: `Bearer ${token.value}`
    }
  }

  function getAuthSignal(signal) {
    const authSignal = authSessionController.signal
    return signal ? AbortSignal.any([signal, authSignal]) : authSignal
  }

  // 用户管理功能
  async function getUsers({ skip = 0, limit = 100, keyword = '', departmentId = null, role = '' } = {}) {
    try {
      return await authApi.getUsers({ skip, limit, keyword, departmentId, role })
    } catch (error) {
      console.error('获取用户列表错误:', error)
      throw error
    }
  }

  async function createUser(userData) {
    try {
      return await authApi.createUser(userData)
    } catch (error) {
      console.error('创建用户错误:', error)
      throw error
    }
  }

  async function updateUser(userId, userData) {
    try {
      return await authApi.updateUser(userId, userData)
    } catch (error) {
      console.error('更新用户错误:', error)
      throw error
    }
  }

  async function deleteUser(userId) {
    try {
      return await authApi.deleteUser(userId)
    } catch (error) {
      console.error('删除用户错误:', error)
      throw error
    }
  }

  // 验证用户名并生成uid
  async function validateUsernameAndGenerateUid(username) {
    try {
      return await authApi.validateUsername(username)
    } catch (error) {
      console.error('用户名验证错误:', error)
      throw error
    }
  }

  // 上传头像
  async function uploadAvatar(file) {
    try {
      const data = await authApi.uploadAvatar(file)

      // 更新本地头像状态
      avatar.value = data.avatar_url

      return data
    } catch (error) {
      console.error('头像上传错误:', error)
      throw error
    }
  }

  // 获取当前用户信息
  async function getCurrentUser(signal = authSessionController.signal) {
    try {
      const userData = await authApi.getCurrentUser(signal)
      signal.throwIfAborted()

      // 更新本地状态
      userId.value = userData.id
      username.value = userData.username
      displayName.value = userData.display_name || ''
      uid.value = userData.uid
      phoneNumber.value = userData.phone_number || ''
      avatar.value = userData.avatar || ''
      userRoles.value = userData.roles || []
      effectivePermissions.value = userData.effective_permissions || []
      departmentId.value = userData.department_id || null
      departmentName.value = userData.department_name || ''

      return userData
    } catch (error) {
      console.error('获取用户信息错误:', error)
      throw error
    }
  }

  /** 接受 OA 下发的 Yuxi bearer，并通过当前用户接口确认其有效性。 */
  async function acceptEmbedToken(accessToken) {
    if (typeof accessToken !== 'string' || !accessToken.trim()) {
      throw new Error('OA 未提供有效访问令牌')
    }

    token.value = accessToken
    localStorage.setItem('user_token', accessToken)
    try {
      const userData = await getCurrentUser()
      console.info('[认证诊断] OA token 已通过 /api/auth/me 校验', {
        userId: userData?.id ?? null
      })
      return userData
    } catch (error) {
      console.error('[认证诊断] OA token 校验失败，将回滚登录态', {
        errorType: error?.name || 'Error',
        status: error?.status ?? null
      })
      if (token.value === accessToken) logout()
      throw error
    }
  }

  // 更新个人资料
  async function updateProfile(profileData) {
    try {
      const userData = await authApi.updateProfile(profileData)

      // 更新本地状态
      if (typeof userData.username === 'string') {
        username.value = userData.username
      }
      if (typeof userData.phone_number !== 'undefined') {
        phoneNumber.value = userData.phone_number || ''
      }

      return userData
    } catch (error) {
      console.error('更新个人资料错误:', error)
      throw error
    }
  }

  return {
    // 状态
    token,
    userId,
    username,
    displayName,
    uid,
    phoneNumber,
    avatar,
    userRoles,
    effectivePermissions,
    departmentId,
    departmentName,

    // 计算属性
    isLoggedIn,
    hasPermission,

    // 方法
    login,
    logout,
    initialize,
    checkFirstRun,
    getAuthHeaders,
    getAuthSignal,
    // 供 OIDC 回调等非表单登录入口复用，统一按勾选状态选择令牌存储位置
    persistToken,
    getUsers,
    createUser,
    updateUser,
    deleteUser,
    validateUsernameAndGenerateUid,
    uploadAvatar,
    acceptEmbedToken,
    getCurrentUser,
    updateProfile
  }
})
