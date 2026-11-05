import { setTestUserRole } from '../helpers/identity.js'
import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import { createPinia, setActivePinia, disposePinia } from 'pinia'
import { createTestServer } from '../helpers/createTestServer.js'

let server, component, authApi, departmentApi, userStore
const pinia = createPinia()
const previousStorage = globalThis.localStorage
before(async () => {
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  setActivePinia(pinia)
  globalThis.__userManagementPage = {
    errors: [], confirmations: [],
    roles: [{ id: 1, code: 'user', name: '员工', is_active: true, default_scope_type: 'self' },
      { id: 2, code: 'reviewer', name: '审核员', is_active: true, default_scope_type: 'organization_and_descendants' }]
  }
  server = await createTestServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', ssr: { noExternal: ['ant-design-vue'] }, plugins: [{
    name: 'user-management-boundaries', enforce: 'pre',
    resolveId(id) { if (id === 'ant-design-vue') return '\0user-management-ant' },
    load(id) {
      if (id === '\0user-management-ant') return 'export const message = { error: (text) => globalThis.__userManagementPage.errors.push(text), success() {} }; export const Modal = { confirm: (options) => globalThis.__userManagementPage.confirmations.push(options) }'
      if (id.endsWith('/src/apis/role_api.js')) return 'export const getRoleOverview = async () => ({ roles: globalThis.__userManagementPage.roles, data_scope_types: [{ key: "self", label: "本人" }, { key: "organization_and_descendants", label: "组织及下级" }] })'
    }
  }] })
  ;({ authApi, departmentApi } = await server.ssrLoadModule('/src/apis/index.js'))
  const { useUserStore } = await server.ssrLoadModule('/src/modules/identity/model/user.js')
  userStore = useUserStore(pinia)
  setTestUserRole(userStore, 'superadmin')
  userStore.effectivePermissions.push('user:create', 'user:update', 'user:delete', 'user:role_assign', 'role:read')
  userStore.userId = 1
  ;({ default: component } = await server.ssrLoadModule('/src/modules/settings/ui/UserManagementComponent.vue'))
})
after(async () => {
  await server?.close()
  disposePinia(pinia)
  delete globalThis.__userManagementPage
  if (previousStorage === undefined) delete globalThis.localStorage
  else globalThis.localStorage = previousStorage
})
const renderer = createRenderer({
  createElement: () => ({ children: [] }), createText: () => ({}), createComment: () => ({}),
  insert: (child, parent) => parent.children.push(child), remove() {}, setText() {}, setElementText() {},
  parentNode: () => null, nextSibling: () => null, patchProp() {}
})

/** 运行完整组件及真实筛选 watcher，隔离网络接口。 */
async function setupPage(requests) {
  const previousUsers = authApi.getUsersPage, previousDepartments = departmentApi.getDepartments
  authApi.getUsersPage = async (params) => { requests.push(params); return { items: [], total: 0 } }
  departmentApi.getDepartments = async () => []
  let page
  const app = renderer.createApp({ setup() {
    page = component.setup({}, { expose() {} })
    return () => null
  } })
  app.use(pinia)
  app.provide(ssrContextKey, {})
  app.mount({ children: [] })
  await nextTick()
  await Promise.resolve()
  return { page, close() {
    app.unmount()
    authApi.getUsersPage = previousUsers
    departmentApi.getDepartments = previousDepartments
  } }
}

test('用户类型默认排除 APP 身份，切换类型回到第一页并保留搜索条件', async () => {
  const requests = []
  const { page, close } = await setupPage(requests)
  try {
    const endUser = { id: 2, user_kind: 'end_user', role: 'user', username: 'app-visitor' }
    assert.equal(page.isUserEditDisabled(endUser), true)
    await page.showEditUserModal(endUser)
    assert.equal(page.userManagement.modalVisible, false)
    assert.equal(page.userManagement.editUserId, null)
    const human = { ...endUser, user_kind: 'human', username: 'product-user' }
    assert.equal(page.isUserEditDisabled(human), false)
    await page.showEditUserModal(human)
    assert.equal(page.userManagement.modalVisible, true)
    assert.equal(page.userManagement.form.username, 'product-user')
    await page.fetchUsers()
    assert.equal(requests[0].userKind, 'human')
    page.userManagement.currentPage = 3
    page.userManagement.searchKeyword = 'visitor'
    page.userManagement.userKindFilter = 'end_user'
    await nextTick()
    assert.deepEqual([page.userManagement.searchKeyword, page.userManagement.departmentFilterValues, page.userManagement.roleFilterValues, page.userManagement.userKindFilter], ['visitor', [], [], 'end_user'])
    assert.equal(page.userManagement.currentPage, 1)
    assert.equal(page.hasActiveFilters.value, true)
    await page.fetchUsers()
    assert.deepEqual(requests.at(-1), {
      offset: 0, limit: 10, search: 'visitor', departmentIds: [], roles: [], userKind: 'end_user'
    })
    page.userManagement.userKindFilter = 'all'
    await page.fetchUsers()
    assert.equal(requests.at(-1).userKind, 'all')
  } finally {
    close()
  }
})

test('企业筛选保留组织树与动态角色多选，切换 APP 后清除隐藏条件', async () => {
  const requests = []
  const { page, close } = await setupPage(requests)
  try {
    assert.deepEqual(page.departmentTree.value, [])
    page.departmentManagement.departments = [
      { id: 1, name: '集团', parent_id: null, user_count: 0 },
      { id: 2, name: '研发部', parent_id: 1, user_count: 3 }
    ]
    assert.equal(page.departmentTree.value[0].name, '集团')
    assert.equal(page.departmentTree.value[0].children[0].name, '研发部')
    assert.deepEqual(page.roleFilterOptions.value.map((role) => role.code), ['user', 'reviewer'])
    page.userManagement.departmentFilterValues = [1, 2]
    page.userManagement.roleFilterValues = ['user', 'reviewer']
    page.userManagement.currentPage = 4
    await nextTick()
    await page.fetchUsers()
    assert.deepEqual(requests.at(-1).departmentIds, [1, 2])
    assert.deepEqual(requests.at(-1).roles, ['user', 'reviewer'])
    assert.equal(page.userManagement.currentPage, 1)
    page.userManagement.userKindFilter = 'end_user'
    await nextTick()
    await page.fetchUsers()
    assert.equal(page.showOrganizationFilters.value, false)
    assert.deepEqual(requests.at(-1).departmentIds, [])
    assert.deepEqual(requests.at(-1).roles, [])
    page.resetFilters()
    await nextTick()
    assert.equal(page.userManagement.userKindFilter, 'human')
    assert.equal(page.showOrganizationFilters.value, true)
  } finally { close() }
})

test('APP 用户拒绝直接编辑和角色提交，删除仍遵守后端返回的可删除范围', async () => {
  const { page, close } = await setupPage([])
  const previousUpdate = userStore.updateUser
  let writes = 0
  userStore.updateUser = async () => { writes++ }
  try {
    const appUser = { id: 2, user_kind: 'end_user', app_id: 'app-owned', username: 'visitor' }
    await page.showEditUserModal(appUser)
    assert.equal(page.userManagement.modalVisible, false)
    page.userManagement.editMode = true
    page.userManagement.editUserId = appUser.id
    page.userManagement.editUserKind = 'end_user'
    page.userManagement.form.username = 'visitor'
    page.userManagement.form.roleAssignments = [{ role_id: 2, scope_mode: 'inherit' }]
    assert.equal(page.canEditRoleAssignments.value, false)
    await page.handleUserFormSubmit()
    assert.equal(writes, 0)
    assert.equal(globalThis.__userManagementPage.errors.at(-1), 'APP 终端用户不支持编辑或分配企业角色')
    assert.equal(page.isUserDeleteDisabled(appUser), false)
    assert.equal(page.isUserDeleteDisabled({ ...appUser, can_delete: false }), true)
    const before = globalThis.__userManagementPage.confirmations.length
    page.confirmDeleteUser({ ...appUser, can_delete: false })
    assert.equal(globalThis.__userManagementPage.confirmations.length, before)
  } finally { userStore.updateUser = previousUpdate; close() }
})

test('企业用户编辑保存多个角色与组织范围，不因 APP 筛选合并回退为单角色', async () => {
  const { page, close } = await setupPage([])
  const previousUpdate = userStore.updateUser
  const writes = []
  userStore.updateUser = async (...args) => writes.push(args)
  try {
    await page.showEditUserModal({ id: 9, uid: 'employee_9', username: '员工九', user_kind: 'human',
      department_id: 2, roles: [
        { id: 1, code: 'user', scope_mode: 'inherit' },
        { id: 2, code: 'reviewer', scope_mode: 'override', override_scope_type: 'self', override_department_ids: [] }
      ] })
    assert.equal(page.userManagement.form.roleAssignments.length, 2)
    await page.handleUserFormSubmit()
    assert.equal(writes.length, 1)
    assert.equal(writes[0][0], 9)
    assert.equal(writes[0][1].department_id, 2)
    assert.deepEqual(writes[0][1].role_assignments, [
      { role_id: 1, scope_mode: 'inherit', override_scope_type: null, override_department_ids: [] },
      { role_id: 2, scope_mode: 'override', override_scope_type: 'self', override_department_ids: [] }
    ])
    assert.equal('role' in writes[0][1], false)
  } finally { userStore.updateUser = previousUpdate; close() }
})

test('切换 APP 类型后迟到的企业列表和编辑准备不重新显示', async () => {
  const { page, close } = await setupPage([])
  const previousUsers = authApi.getUsersPage
  const previousDepartments = departmentApi.getDepartments
  let resolveUsers
  let resolveDepartments
  authApi.getUsersPage = () => new Promise((resolve) => { resolveUsers = resolve })
  departmentApi.getDepartments = () => new Promise((resolve) => { resolveDepartments = resolve })
  try {
    const pendingList = page.fetchUsers()
    const pendingEdit = page.showEditUserModal({ id: 7, user_kind: 'human', username: '员工七', roles: [] })
    page.userManagement.userKindFilter = 'end_user'
    await nextTick()
    resolveUsers({ items: [{ id: 7, user_kind: 'human' }], total: 1 })
    resolveDepartments([])
    await Promise.all([pendingList, pendingEdit])
    assert.deepEqual(page.userManagement.users, [])
    assert.equal(page.userManagement.modalVisible, false)
  } finally {
    authApi.getUsersPage = previousUsers
    departmentApi.getDepartments = previousDepartments
    close()
  }
})

test('管理权限失效后直接调用表单入口也不提交或打开编辑', async () => {
  const { page, close } = await setupPage([])
  const permissions = [...userStore.effectivePermissions]
  const previousCreate = userStore.createUser
  let writes = 0
  userStore.createUser = async () => { writes++ }
  try {
    userStore.effectivePermissions = []
    await page.showAddUserModal()
    await page.showEditUserModal({ id: 3, username: '员工三', user_kind: 'human' })
    assert.equal(page.userManagement.modalVisible, false)
    page.userManagement.form.username = '员工三'
    await page.handleUserFormSubmit()
    assert.equal(writes, 0)
    assert.equal(page.isUserDeleteDisabled({ id: 3, user_kind: 'end_user' }), true)
    assert.equal(globalThis.__userManagementPage.errors.at(-1), '没有管理此用户的权限')
  } finally {
    userStore.effectivePermissions = permissions
    userStore.createUser = previousCreate
    close()
  }
})
