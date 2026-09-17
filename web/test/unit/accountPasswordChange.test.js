import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'

// 普通用户在「账户设置」页自助修改登录密码的装配链：
// auth_api 暴露 changePassword → 组件收集三个输入并调用接口 → 字段名与后端 UserPasswordChange 一致。
// 这里按项目既有风格用源码断言固定契约，字段名写错会直接让接口 422，属于必须在测试里钉住的点。
const readSource = (relativePath) =>
  readFileSync(new URL(`../../src/${relativePath}`, import.meta.url), 'utf8')

test('auth_api 暴露 changePassword 并请求 PUT /api/auth/password', () => {
  const source = readSource('apis/auth_api.js')

  assert.match(
    source,
    /async function changePassword\(passwordData\) \{\s*return apiPut\('\/api\/auth\/password', passwordData\)/
  )
  // 必须挂到导出对象上，否则组件拿不到这个方法
  assert.match(source, /^\s*changePassword,$/m)
})

test('账户设置页收集原密码/新密码/确认密码并调用 changePassword', () => {
  const source = readSource('components/AccountSettingsComponent.vue')

  assert.match(source, /v-model:value="passwordDraft\.oldPassword"/)
  assert.match(source, /v-model:value="passwordDraft\.newPassword"/)
  assert.match(source, /v-model:value="passwordDraft\.confirmPassword"/)
  // 字段名必须与后端 UserPasswordChange 的 old_password / new_password 一致，否则接口返回 422
  assert.match(source, /authApi\.changePassword\(\{ old_password: oldPassword, new_password: newPassword \}\)/)
  // 成功后清空输入框，避免明文停留在页面上
  assert.match(source, /resetPasswordDraft\(\)/)
  assert.match(source, /message\.success\('密码修改成功'\)/)
})

test('前端校验口径与后端一致：新密码至少 8 位、两次输入相同、不能与原密码相同', () => {
  const source = readSource('components/AccountSettingsComponent.vue')

  // 常量值必须与后端 UserPasswordChange 的 min_length=8 对齐
  assert.match(source, /const MIN_PASSWORD_LENGTH = 8/)
  assert.match(source, /newPassword\.length < MIN_PASSWORD_LENGTH/)
  assert.match(source, /newPassword !== passwordDraft\.confirmPassword/)
  assert.match(source, /newPassword === oldPassword/)
})

test('改密入口是资料卡片里的按钮 + 弹窗，不是常驻表单', () => {
  const source = readSource('components/AccountSettingsComponent.vue')

  // 账户资料卡片内保留一个「修改密码」按钮作为入口
  assert.match(source, /@click="openPasswordModal">修改密码</)
  // 表单必须放在弹窗里，弹窗开合由 passwordModalOpen 控制
  assert.match(source, /v-model:open="passwordModalOpen"/)
  assert.match(source, /@ok="submitPasswordChange"/)
  assert.match(source, /@cancel="closePasswordModal"/)
  // 打开前先清空，关闭时也清空，避免明文残留
  assert.match(source, /const openPasswordModal = \(\) => \{\s*resetPasswordDraft\(\)\s*passwordModalOpen\.value = true/)
  assert.match(source, /const closePasswordModal = \(\) => \{\s*passwordModalOpen\.value = false\s*resetPasswordDraft\(\)/)
  // 成功后关闭弹窗（而不是只清空表单）
  assert.match(source, /message\.success\('密码修改成功'\)\s*closePasswordModal\(\)/)
  // 反向约束：不要再把表单直接铺在页面卡片里
  assert.doesNotMatch(source, /security-card/)
})

test('原密码错误时给出可理解的提示（后端 400 细节被 base.js 统一屏蔽）', () => {
  const source = readSource('components/AccountSettingsComponent.vue')

  // 400 一律来自「原密码不正确」，补一句人话提示；其余状态码沿用通用错误文案
  assert.match(source, /error\?\.status === 400 \? '请确认原密码是否正确'/)
  assert.match(source, /message\.error\(`修改失败：\$\{reason\}`\)/)
})
