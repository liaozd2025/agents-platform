import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('根路由直接进入受保护的智能体入口', () => {
  const routerSource = readFileSync(new URL('../../src/router/index.js', import.meta.url), 'utf8')

  assert.match(routerSource, /path:\s*['"]\/['"],\s*redirect:\s*['"]\/agent['"]/)
  assert.doesNotMatch(routerSource, /BlankLayout|HomeView/)
})

test('组织名与品牌名相同时登录页不重复显示', () => {
  const loginSource = readFileSync(new URL('../../src/views/LoginView.vue', import.meta.url), 'utf8')

  assert.match(
    loginSource,
    /<span v-if="brandName !== brandOrgName" class="brand-main">\{\{ brandName \}\}<\/span>/
  )
})
