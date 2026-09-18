<template>
  <div class="account-settings">
    <div class="header-section">
      <div class="header-content">
        <div class="section-title">账户设置</div>
        <p class="section-description">管理当前账户资料、身份信息。</p>
      </div>
      <a-button class="lucide-icon-btn" :loading="refreshing" @click="refreshProfile">
        <template #icon><RefreshCw :size="16" :class="{ spin: refreshing }" /></template>
        刷新
      </a-button>
    </div>

    <div class="account-card profile-card">
      <div class="profile-summary">
        <div class="profile-left">
          <a-upload
            :show-upload-list="false"
            :before-upload="beforeUpload"
            @change="handleAvatarChange"
            accept="image/*"
          >
            <div class="avatar-upload" :class="{ uploading: avatarUploading }">
              <FallbackAvatar
                :src="userStore.avatar"
                :default-src="avatarDefaultSrc"
                :name="userStore.username"
                :seed="userStore.uid || userStore.username"
                kind="user"
                :size="80"
                shape="circle"
                :alt="userStore.username"
                class="account-avatar"
              />
              <div class="avatar-mask">
                <Upload v-if="!avatarUploading" :size="16" />
                <RefreshCw v-else :size="16" class="spin" />
                <span>{{ userStore.avatar ? '更换' : '上传' }}</span>
              </div>
            </div>
          </a-upload>

          <div class="profile-fields">
            <div class="profile-row editable-row">
              <span class="profile-label">用户名</span>
              <a-input
                v-if="editingField === 'username'"
                ref="usernameInput"
                v-model:value="profileDraft.username"
                class="inline-input"
                size="small"
                :max-length="20"
                :disabled="savingField === 'username'"
                @press-enter="saveField('username')"
                @keydown.esc.stop.prevent="cancelField"
                @blur="cancelField"
              />
              <button
                v-else
                type="button"
                class="editable-value"
                @click="startFieldEdit('username')"
              >
                {{ userStore.username || '未设置' }}
              </button>
            </div>
            <div class="profile-row editable-row">
              <span class="profile-label">手机号</span>
              <a-input
                v-if="editingField === 'phone_number'"
                ref="phoneInput"
                v-model:value="profileDraft.phone_number"
                class="inline-input"
                size="small"
                :max-length="11"
                :disabled="savingField === 'phone_number'"
                @press-enter="saveField('phone_number')"
                @keydown.esc.stop.prevent="cancelField"
                @blur="cancelField"
              />
              <button
                v-else
                type="button"
                class="editable-value"
                @click="startFieldEdit('phone_number')"
              >
                {{ userStore.phoneNumber || '未设置' }}
              </button>
            </div>
            <div class="profile-row">
              <span class="profile-label">UID</span>
              <span class="profile-value mono">{{ userStore.uid || '未设置' }}</span>
            </div>
            <div class="profile-row">
              <span class="profile-label">密码</span>
              <button type="button" class="editable-value" @click="openPasswordModal">修改密码</button>
            </div>
          </div>
        </div>

        <div class="identity-panel">
          <div class="identity-item">
            <span class="identity-icon"><ShieldCheck :size="15" /></span>
            <span class="profile-label">权限</span>
            <span class="profile-value">
              {{ assignedRolesText }}
            </span>
          </div>
          <div class="identity-item">
            <span class="identity-icon"><Building2 :size="15" /></span>
            <span class="profile-label">部门</span>
            <span class="profile-value">{{ userStore.departmentName || '默认部门' }}</span>
          </div>
        </div>
      </div>
      <UserConfigSettingsCard ref="userConfigRef" />
    </div>

    <!-- 修改密码弹窗：由账户资料卡片里的「修改密码」按钮打开，走 PUT /api/auth/password（需填原密码） -->
    <a-modal
      v-model:open="passwordModalOpen"
      title="修改密码"
      :confirmLoading="changingPassword"
      okText="保存新密码"
      cancelText="取消"
      :maskClosable="false"
      width="420px"
      class="password-modal"
      @ok="submitPasswordChange"
      @cancel="closePasswordModal"
    >
      <a-form layout="vertical" class="password-form" @submit.prevent>
        <a-form-item label="原密码">
          <a-input-password
            v-model:value="passwordDraft.oldPassword"
            :maxlength="64"
            placeholder="请输入当前登录密码"
            autocomplete="current-password"
          />
        </a-form-item>
        <a-form-item label="新密码">
          <a-input-password
            v-model:value="passwordDraft.newPassword"
            :maxlength="64"
            :placeholder="`请输入新密码（至少 ${MIN_PASSWORD_LENGTH} 位）`"
            autocomplete="new-password"
          />
        </a-form-item>
        <a-form-item label="确认新密码">
          <a-input-password
            v-model:value="passwordDraft.confirmPassword"
            :maxlength="64"
            placeholder="请再次输入新密码"
            autocomplete="new-password"
            @press-enter="submitPasswordChange"
          />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup>
import UserConfigSettingsCard from '@/components/UserConfigSettingsCard.vue'

import { computed, nextTick, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { Building2, RefreshCw, ShieldCheck, Upload } from '@lucide/vue'
import { authApi } from '@/apis/auth_api'
import FallbackAvatar from '@/components/common/FallbackAvatar.vue'
import { useUserStore } from '@/stores/user'
import { generatePixelAvatar } from '@/utils/pixelAvatar'

const userStore = useUserStore()
const avatarUploading = ref(false)
const refreshing = ref(false)
const savingField = ref('')
const editingField = ref('')
const usernameInput = ref(null)
const phoneInput = ref(null)
const userConfigRef = ref(null)
const profileDraft = reactive({
  username: '',
  phone_number: ''
})

// 新密码最短长度，与后端 UserPasswordChange 的 min_length=8 保持一致
const MIN_PASSWORD_LENGTH = 8
const changingPassword = ref(false)
const passwordModalOpen = ref(false)
const passwordDraft = reactive({
  oldPassword: '',
  newPassword: '',
  confirmPassword: ''
})

const avatarDefaultSrc = computed(() => (userStore.uid ? generatePixelAvatar(userStore.uid) : ''))

const assignedRolesText = computed(
  () => userStore.userRoles.map((role) => role.name).join('、') || '未分配角色'
)

const syncProfileDraft = () => {
  profileDraft.username = userStore.username || ''
  profileDraft.phone_number = userStore.phoneNumber || ''
}

const refreshProfile = async () => {
  refreshing.value = true
  try {
    await Promise.all([userStore.getCurrentUser(), userConfigRef.value?.refresh?.()])
    syncProfileDraft()
    message.success('账户设置已刷新')
  } catch (error) {
    console.error('刷新用户信息失败:', error)
    message.error('刷新失败：' + (error.message || '请稍后重试'))
  } finally {
    refreshing.value = false
  }
}

const startFieldEdit = async (field) => {
  syncProfileDraft()
  editingField.value = field
  await nextTick()
  const inputRef = field === 'username' ? usernameInput.value : phoneInput.value
  inputRef?.focus?.()
}

const cancelField = () => {
  if (savingField.value) return
  editingField.value = ''
  syncProfileDraft()
}

const saveField = async (field) => {
  const payload = {}
  if (field === 'username') {
    const username = profileDraft.username.trim()
    if (username.length < 2 || username.length > 20) {
      message.error('用户名长度必须在 2-20 个字符之间')
      return
    }
    if (username === userStore.username) {
      cancelField()
      return
    }
    payload.username = username
  }

  if (field === 'phone_number') {
    const phoneNumber = profileDraft.phone_number.trim()
    if (phoneNumber && !validatePhoneNumber(phoneNumber)) {
      message.error('请输入正确的手机号格式')
      return
    }
    if (phoneNumber === (userStore.phoneNumber || '')) {
      cancelField()
      return
    }
    payload.phone_number = phoneNumber
  }

  savingField.value = field
  try {
    await userStore.updateProfile(payload)
    syncProfileDraft()
    editingField.value = ''
    message.success('个人资料更新成功')
  } catch (error) {
    console.error('更新个人资料失败:', error)
    message.error('更新失败：' + (error.message || '请稍后重试'))
  } finally {
    savingField.value = ''
  }
}

// 清空密码输入框，避免明文长期停留在页面上
const resetPasswordDraft = () => {
  passwordDraft.oldPassword = ''
  passwordDraft.newPassword = ''
  passwordDraft.confirmPassword = ''
}

// 打开弹窗前先清空，避免上次失败留下的内容被误提交
const openPasswordModal = () => {
  resetPasswordDraft()
  passwordModalOpen.value = true
}

const closePasswordModal = () => {
  passwordModalOpen.value = false
  resetPasswordDraft()
}

const submitPasswordChange = async () => {
  const oldPassword = passwordDraft.oldPassword
  const newPassword = passwordDraft.newPassword

  // 前端只做必要的即时提示，最终规则以后端校验为准
  if (!oldPassword) {
    message.error('请输入原密码')
    return
  }
  if (newPassword.length < MIN_PASSWORD_LENGTH) {
    message.error(`新密码至少 ${MIN_PASSWORD_LENGTH} 位`)
    return
  }
  if (newPassword === oldPassword) {
    message.error('新密码不能与原密码相同')
    return
  }
  if (newPassword !== passwordDraft.confirmPassword) {
    message.error('两次输入的新密码不一致')
    return
  }

  changingPassword.value = true
  try {
    await authApi.changePassword({ old_password: oldPassword, new_password: newPassword })
    // 成功后关闭弹窗并清空表单；当前登录态不受影响，下次登录使用新密码
    message.success('密码修改成功')
    closePasswordModal()
  } catch (error) {
    // 失败时保留弹窗，方便用户直接修正原密码。
    // base.js 出于防泄露考虑会把后端 400 的 detail 统一替换成「请求参数错误」，
    // 因此这里按状态码补一句本表单最常见的失败原因，否则用户不知道错在哪。
    console.error('修改密码失败:', error)
    const reason = error?.status === 400 ? '请确认原密码是否正确' : error.message || '请稍后重试'
    message.error(`修改失败：${reason}`)
  } finally {
    changingPassword.value = false
  }
}

const validatePhoneNumber = (phone) => {
  if (!phone) return true
  const phoneRegex = /^1[3-9]\d{9}$/
  return phoneRegex.test(phone)
}

const beforeUpload = (file) => {
  const isImage = file.type.startsWith('image/')
  if (!isImage) {
    message.error('只能上传图片文件！')
    return false
  }

  const isLt5M = file.size / 1024 / 1024 < 5
  if (!isLt5M) {
    message.error('图片大小不能超过 5MB！')
    return false
  }

  return true
}

const handleAvatarChange = async (info) => {
  if (info.file.status === 'uploading') {
    avatarUploading.value = true
    return
  }

  if (info.file.status === 'done') {
    avatarUploading.value = false
    return
  }

  try {
    avatarUploading.value = true
    await userStore.uploadAvatar(info.file.originFileObj || info.file)
    message.success('头像上传成功！')
  } catch (error) {
    console.error('头像上传失败:', error)
    message.error('头像上传失败：' + (error.message || '请稍后重试'))
  } finally {
    avatarUploading.value = false
  }
}

watch(() => [userStore.username, userStore.phoneNumber], syncProfileDraft, { immediate: true })
</script>

<style lang="less" scoped>
.account-settings {
  display: flex;
  flex-direction: column;
  gap: 16px;

  .account-card {
    padding: 18px;
    border-radius: 12px;
    background: var(--gray-0);
    border: 1px solid var(--gray-150);
  }

  .profile-card {
    display: flex;
    flex-direction: column;
    gap: 18px;
    background: var(--gray-25);
  }

  // 安全设置相关样式已随「改成弹窗」移除：弹窗内容由 antd 挂到 body 下，
  // 其表单样式写在根层级（见文件末尾 .password-form），不能嵌在 .account-settings 里。

  .profile-summary {
    display: flex;
    align-items: stretch;
    justify-content: space-between;
    gap: 20px;

    @media (max-width: 760px) {
      flex-direction: column;
    }
  }

  .profile-left {
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 18px;
    flex: 1;

    @media (max-width: 520px) {
      align-items: flex-start;
      flex-direction: column;
    }
  }

  .avatar-upload {
    width: 80px;
    height: 80px;
    position: relative;
    cursor: pointer;
    border-radius: 50%;
    overflow: hidden;
    flex: 0 0 auto;

    .account-avatar {
      width: 80px;
      height: 80px;
      border: 3px solid var(--gray-0);
    }

    &:hover .avatar-mask,
    &.uploading .avatar-mask {
      opacity: 1;
    }
  }

  .avatar-mask {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 4px;
    color: var(--gray-0);
    font-size: 12px;
    background: rgba(0, 0, 0, 0.48);
    opacity: 0;
    transition: opacity 0.2s ease;
  }

  .profile-fields {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
    flex: 1;
  }

  .profile-row {
    min-width: 0;
    display: grid;
    grid-template-columns: 56px minmax(0, 1fr);
    align-items: center;
    gap: 10px;
  }

  .profile-label {
    color: var(--gray-600);
    font-size: 13px;
    flex-shrink: 0;
  }

  .profile-value,
  .editable-value {
    min-width: 0;
    color: var(--gray-900);
    font-size: 14px;
    font-weight: 500;
    line-height: 24px;
    overflow: hidden;
    text-align: left;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .editable-value {
    width: fit-content;
    max-width: 100%;
    padding: 0 6px;
    margin-left: -6px;
    border: none;
    border-radius: 6px;
    background: transparent;
    cursor: pointer;

    &:hover {
      color: var(--main-color);
      background: var(--main-5);
    }
  }

  .inline-input {
    width: min(260px, 100%);
  }

  .identity-panel {
    min-width: 220px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 12px;
    padding: 14px;
    border-radius: 10px;
    background: var(--gray-0);

    @media (max-width: 760px) {
      min-width: 0;
    }
  }

  .identity-item {
    min-width: 0;
    display: grid;
    grid-template-columns: 20px 42px minmax(0, 1fr);
    align-items: center;
    gap: 8px;
  }

  .identity-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    border-radius: 6px;
    background: var(--gray-50);
    color: var(--gray-500);
  }

  .mono {
    font-family: 'Monaco', 'Consolas', monospace;
  }

  .apikey-card {
    padding: 16px;
  }
}

:deep(.spin) {
  animation: spin 1s linear infinite;
}

// 修改密码弹窗的表单：弹窗由 antd 挂到 body 下，写成根层级选择器才能命中
.password-form {
  :deep(.ant-form-item) {
    margin-bottom: 14px;
  }
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }

  to {
    transform: rotate(360deg);
  }
}
</style>
