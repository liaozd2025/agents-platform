<template>
  <section class="role-management">
    <a-spin :spinning="loading">
      <div v-if="errorMessage" class="role-load-error" role="alert">
        <span>{{ errorMessage }}</span>
        <a-button size="small" @click="loadOverview()">重新加载</a-button>
      </div>

      <template v-else-if="overview.roles.length">
        <div v-if="viewMode === 'list'" class="role-list-view">
          <header class="role-list-header">
            <div>
              <h1>角色与权限</h1>
              <p>
                选择一个角色查看或编辑它的菜单权限、操作权限、数据范围和成员。内置角色受系统保护，自定义角色可停用而非删除。
              </p>
            </div>
            <a-button v-if="canManageRoles" type="primary" @click="openCreate">
              <Plus :size="16" />
              创建角色
            </a-button>
          </header>

          <div class="role-filter-bar">
            <a-input
              v-model:value="roleSearch"
              allow-clear
              placeholder="搜索角色名称或标识..."
              aria-label="搜索角色"
              class="role-search"
            >
              <template #prefix><Search :size="16" /></template>
            </a-input>

            <div class="role-kind-filter" aria-label="角色类型筛选">
              <button
                v-for="filter in roleKindFilters"
                :key="filter.value"
                type="button"
                :class="{ active: roleKind === filter.value }"
                @click="roleKind = filter.value"
              >
                {{ filter.label }}
              </button>
            </div>

            <span class="role-list-summary">
              {{ filteredRoles.length }} 个角色 · {{ menuPermissionCatalog.length }} 个菜单 ·
              {{ operationPermissionCatalog.length }} 项操作权限
            </span>
          </div>

          <div v-if="filteredRoles.length" class="role-table-scroll">
            <div class="role-table" role="table" aria-label="角色总览">
              <div class="role-table-header" role="row">
                <span role="columnheader">角色</span>
                <span role="columnheader">数据范围</span>
                <span role="columnheader">菜单权限</span>
                <span role="columnheader">操作权限</span>
                <span role="columnheader">成员</span>
                <span role="columnheader">状态</span>
                <span aria-hidden="true"></span>
              </div>

              <button
                v-for="role in filteredRoles"
                :key="role.id"
                type="button"
                class="role-table-row"
                role="row"
                @click="openRole(role.id)"
              >
                <span class="role-identity" role="cell">
                  <span class="role-name-line">
                    <strong>{{ role.name }}</strong>
                    <span class="role-kind" :class="role.is_builtin ? 'builtin' : 'custom'">
                      {{ role.is_builtin ? '内置' : '自定义' }}
                    </span>
                  </span>
                  <code :title="role.code">{{ role.code }}</code>
                </span>
                <span role="cell">{{ getRoleScopeSummary(role) }}</span>
                <span role="cell">
                  {{ countRolePermissions(role.permission_keys, false) }} /
                  {{ menuPermissionCatalog.length }}
                </span>
                <span role="cell">
                  {{ countRolePermissions(role.permission_keys, true) }} /
                  {{ operationPermissionCatalog.length }}
                </span>
                <span role="cell">{{ role.member_count }} 人</span>
                <span role="cell">
                  <span class="role-state" :class="{ disabled: !role.is_active }">
                    {{ role.is_active ? '已启用' : '已停用' }}
                  </span>
                </span>
                <span class="role-view-link" role="cell">
                  查看
                  <ChevronRight :size="16" />
                </span>
              </button>
            </div>
          </div>

          <a-empty v-else :image="false" description="没有匹配的角色，请调整搜索或筛选条件" />
        </div>

        <article v-else-if="selectedRole" class="role-detail-view">
          <nav class="role-breadcrumb" aria-label="面包屑">
            <button type="button" @click="backToList">角色与权限</button>
            <span>/</span>
            <span>{{ selectedRole.name }}</span>
          </nav>

          <header class="role-detail-header">
            <div class="role-detail-copy">
              <div class="role-title-row">
                <h1>{{ selectedRole.name }}</h1>
                <span class="role-kind" :class="selectedRole.is_builtin ? 'builtin' : 'custom'">
                  {{ selectedRole.is_builtin ? '内置角色' : '自定义角色' }}
                </span>
                <span class="role-state" :class="{ disabled: !selectedRole.is_active }">
                  {{ selectedRole.is_active ? '已启用' : '已停用' }}
                </span>
                <code>{{ selectedRole.code }}</code>
              </div>
              <p>{{ selectedRole.description || '暂无说明' }}</p>
            </div>

            <a-space v-if="canManageRoles && !inlineEditing" wrap>
              <a-button @click="backToList">
                <ArrowLeft :size="16" />
                返回列表
              </a-button>
              <a-button @click="openCopy">
                <Copy :size="16" />
                复制角色
              </a-button>
              <a-button v-if="!selectedRole.is_builtin" @click="openEdit">
                <SquarePen :size="16" />
                编辑角色
              </a-button>
              <a-button
                v-if="!selectedRole.is_builtin && selectedRole.is_active"
                danger
                @click="confirmDeactivate"
              >
                <Ban :size="16" />
                停用
              </a-button>
            </a-space>
            <a-button v-else-if="!canManageRoles" @click="backToList">
              <ArrowLeft :size="16" />
              返回列表
            </a-button>
          </header>

          <div class="role-summary-grid">
            <div>
              <span>默认数据范围</span>
              <strong>{{ selectedScopeSummary }}</strong>
            </div>
            <div>
              <span>菜单权限</span>
              <strong>
                {{ countRolePermissions(selectedRole.permission_keys, false) }} /
                {{ menuPermissionCatalog.length }}
              </strong>
            </div>
            <div>
              <span>操作权限</span>
              <strong>
                {{ countRolePermissions(selectedRole.permission_keys, true) }} /
                {{ operationPermissionCatalog.length }}
              </strong>
            </div>
            <div>
              <span>当前成员</span>
              <strong>{{ selectedRole.member_count }} 人</strong>
            </div>
          </div>

          <div class="role-detail-tabs" role="tablist" aria-label="角色详情">
            <button
              type="button"
              role="tab"
              :aria-selected="detailTab === 'permissions'"
              :class="{ active: detailTab === 'permissions' }"
              @click="detailTab = 'permissions'"
            >
              功能权限
            </button>
            <button
              type="button"
              role="tab"
              :aria-selected="detailTab === 'members'"
              :class="{ active: detailTab === 'members' }"
              @click="openMembersTab"
            >
              成员 {{ selectedRole.member_count }} 人
            </button>
          </div>

          <section v-if="detailTab === 'permissions'" class="role-panel permission-panel">
            <header class="role-panel-header">
              <div>
                <p>
                  {{
                    inlineEditing
                      ? '取消菜单会一并取消其下操作权限；勾选操作会自动补上所属菜单。'
                      : selectedRole.is_builtin
                        ? '内置角色的权限由系统维护，如需调整请复制为自定义角色。'
                        : `菜单 ${countRolePermissions(selectedRole.permission_keys, false)} / ${menuPermissionCatalog.length} · 操作 ${countRolePermissions(selectedRole.permission_keys, true)} / ${operationPermissionCatalog.length}`
                  }}
                </p>
                <div class="permission-legend" aria-label="权限类型说明">
                  <span><i class="menu"></i>菜单权限 — 决定左侧导航能看到哪些页面</span>
                  <span><i class="operation"></i>操作权限 — 页面内按钮级的动作</span>
                </div>
              </div>

              <div class="permission-panel-actions">
                <a-space v-if="inlineEditing">
                  <span class="permission-edit-count">
                    菜单 {{ countRolePermissions(inlinePermissionKeys, false) }} · 操作
                    {{ countRolePermissions(inlinePermissionKeys, true) }}
                  </span>
                  <a-button @click="cancelPermissionEdit">取消</a-button>
                  <a-button type="primary" :loading="saving" @click="savePermissions">保存</a-button>
                </a-space>
                <template v-else>
                  <div class="permission-filter" aria-label="权限显示筛选">
                    <button
                      type="button"
                      :class="{ active: !showGrantedOnly }"
                      @click="showGrantedOnly = false"
                    >
                      全部
                    </button>
                    <button
                      type="button"
                      :class="{ active: showGrantedOnly }"
                      @click="showGrantedOnly = true"
                    >
                      仅已授权
                    </button>
                  </div>

                  <a-button
                    v-if="canManageRoles && !selectedRole.is_builtin"
                    @click="startPermissionEdit"
                  >
                    <SquarePen :size="16" />
                    编辑权限
                  </a-button>
                  <span v-else-if="selectedRole.is_builtin" class="permission-locked">
                    <LockKeyhole :size="16" />
                    权限受保护
                  </span>
                </template>
              </div>
            </header>

            <div v-if="displayPermissionGroups.length" class="permission-groups">
              <section v-for="group in displayPermissionGroups" :key="group.label">
                <div class="permission-group-title">
                  <span>{{ group.label }}</span>
                  <i></i>
                </div>
                <div class="permission-menu-list">
                  <article
                    v-for="menu in group.menus"
                    :key="menu.key"
                    class="permission-menu-card"
                    :class="{ granted: isPermissionGranted(menu.key) }"
                  >
                    <button
                      type="button"
                      class="permission-menu-header"
                      :disabled="!inlineEditing"
                      :aria-pressed="isPermissionGranted(menu.key)"
                      @click="toggleInlinePermission(menu.key, !isPermissionGranted(menu.key))"
                    >
                      <span
                        class="permission-check"
                        :class="{ editable: inlineEditing }"
                        aria-hidden="true"
                      >
                        <Check v-if="isPermissionGranted(menu.key)" :size="11" />
                      </span>
                      <strong>{{ menu.name }}</strong>
                      <span class="permission-kind">菜单</span>
                      <code>{{ menu.key }}</code>
                      <span class="permission-operation-summary">
                        {{
                          menu.operation_count
                            ? `操作 ${menu.granted_operation_count} / ${menu.operation_count}`
                            : '无操作权限'
                        }}
                      </span>
                    </button>

                    <div v-if="menu.operations.length" class="permission-operation-grid">
                      <button
                        v-for="operation in menu.operations"
                        :key="operation.key"
                        type="button"
                        class="permission-operation-card"
                        :class="{ granted: isPermissionGranted(operation.key) }"
                        :disabled="!inlineEditing"
                        :aria-pressed="isPermissionGranted(operation.key)"
                        :title="operation.description"
                        @click="
                          toggleInlinePermission(
                            operation.key,
                            !isPermissionGranted(operation.key)
                          )
                        "
                      >
                        <span
                          class="permission-operation-check"
                          :class="{ editable: inlineEditing }"
                          aria-hidden="true"
                        >
                          <Check v-if="isPermissionGranted(operation.key)" :size="9" />
                        </span>
                        <span>{{ operation.name }}</span>
                        <code>{{ permissionShortKey(operation.key) }}</code>
                      </button>
                    </div>
                    <p
                      v-else-if="menu.operation_count && showGrantedOnly"
                      class="permission-empty-operations"
                    >
                      该菜单下的操作权限均未授权
                    </p>
                  </article>
                </div>
              </section>
            </div>
            <a-empty v-else :image="false" description="暂无已授权权限，可切换到全部查看" />
          </section>

          <section v-else-if="detailTab === 'members'" class="role-panel members-panel">
            <header class="member-toolbar">
              <a-input
                v-model:value="memberSearch"
                allow-clear
                placeholder="搜索成员姓名或账号..."
                aria-label="搜索角色成员"
                @change="memberPage = 1"
              >
                <template #prefix><Search :size="16" /></template>
              </a-input>
              <div class="member-toolbar-actions">
                <span>共 {{ filteredMembers.length }} 人</span>
                <a-button :disabled="!filteredMembers.length" @click="exportMembers">导出</a-button>
              </div>
            </header>

            <div v-if="filteredMembers.length" class="member-table" role="table" aria-label="角色成员">
              <div class="member-table-header" role="row">
                <span role="columnheader">成员</span>
                <span role="columnheader">账号</span>
              </div>
              <div v-for="member in pagedMembers" :key="member.id" class="member-row" role="row">
                <span class="member-identity" role="cell">
                  <FallbackAvatar
                    :name="member.username"
                    :seed="member.uid"
                    kind="user"
                    :size="30"
                    shape="circle"
                    :alt="member.username"
                  />
                  <strong>{{ member.username }}</strong>
                </span>
                <code role="cell">{{ member.uid }}</code>
              </div>
              <footer v-if="filteredMembers.length > memberPageSize" class="member-pagination">
                <span>
                  第 {{ (memberPage - 1) * memberPageSize + 1 }} -
                  {{ Math.min(memberPage * memberPageSize, filteredMembers.length) }} 人
                </span>
                <a-pagination
                  v-model:current="memberPage"
                  :total="filteredMembers.length"
                  :page-size="memberPageSize"
                  :show-size-changer="false"
                  size="small"
                />
              </footer>
            </div>
            <a-empty v-else :image="false" description="没有匹配的成员，请调整搜索条件" />
          </section>
        </article>
      </template>

      <a-empty v-else-if="!loading" description="暂无角色" />
    </a-spin>

    <a-modal
      v-model:open="editorOpen"
      :title="editorTitle"
      :confirm-loading="saving"
      width="840px"
      ok-text="保存"
      @ok="saveRole"
    >
      <a-form layout="vertical" class="role-form">
        <div class="role-form-row">
          <a-form-item label="角色名称" required>
            <a-input
              v-model:value="roleForm.name"
              :maxlength="100"
              placeholder="例如：安全审计员"
            />
          </a-form-item>
          <a-form-item label="角色标识" required>
            <a-input
              v-model:value="roleForm.code"
              :disabled="editorMode === 'edit'"
              :maxlength="64"
              placeholder="例如：security_auditor"
            />
          </a-form-item>
        </div>

        <a-form-item label="说明">
          <a-textarea
            v-model:value="roleForm.description"
            :rows="2"
            :maxlength="2000"
            placeholder="这个角色能做什么"
          />
        </a-form-item>

        <a-form-item v-if="editorMode !== 'copy'" label="默认数据范围" required>
          <a-select
            v-model:value="roleForm.default_scope_type"
            :options="scopeOptions"
            @change="handleScopeChange"
          />
        </a-form-item>

        <a-form-item
          v-if="
            editorMode !== 'copy' &&
            roleForm.default_scope_type === 'selected_organizations_and_descendants'
          "
          label="指定组织子树"
          required
        >
          <a-tree-select
            v-model:value="roleForm.default_department_ids"
            :tree-data="departmentTree"
            :field-names="{ label: 'name', value: 'id', children: 'children' }"
            tree-checkable
            tree-default-expand-all
            allow-clear
            placeholder="选择一个或多个组织节点"
            style="width: 100%"
          />
        </a-form-item>

        <a-alert
          v-if="editorMode === 'copy'"
          type="info"
          show-icon
          message="功能权限和默认数据范围将原样复制，创建后可独立编辑。"
        />

        <a-form-item v-else label="菜单与操作权限">
          <div class="permission-editor">
            <div class="permission-editor-summary">
              <span>
                菜单 {{ countRolePermissions(roleForm.permission_keys, false) }} · 操作
                {{ countRolePermissions(roleForm.permission_keys, true) }}
              </span>
              <a-button size="small" @click="roleForm.permission_keys = []">清空</a-button>
            </div>
            <p class="permission-editor-hint">
              勾选菜单后可继续勾选该菜单下的操作权限；取消菜单会一并取消其操作权限。
            </p>
            <section v-for="group in formPermissionGroups" :key="group.label">
              <div class="permission-group-title">
                <span>{{ group.label }}</span>
                <i></i>
              </div>
              <div class="permission-menu-list">
                <article
                  v-for="menu in group.menus"
                  :key="menu.key"
                  class="permission-menu-card"
                  :class="{ granted: isFormPermissionGranted(menu.key) }"
                >
                  <button
                    type="button"
                    class="permission-menu-header"
                    :aria-pressed="isFormPermissionGranted(menu.key)"
                    @click="toggleFormPermission(menu.key, !isFormPermissionGranted(menu.key))"
                  >
                    <span class="permission-check editable" aria-hidden="true">
                      <Check v-if="isFormPermissionGranted(menu.key)" :size="11" />
                    </span>
                    <strong>{{ menu.name }}</strong>
                    <span class="permission-kind">菜单</span>
                    <code>{{ menu.key }}</code>
                    <span class="permission-operation-summary">
                      {{
                        menu.operation_count
                          ? `操作 ${menu.granted_operation_count} / ${menu.operation_count}`
                          : '无操作权限'
                      }}
                    </span>
                  </button>
                  <div v-if="menu.operations.length" class="permission-operation-grid">
                    <button
                      v-for="operation in menu.operations"
                      :key="operation.key"
                      type="button"
                      class="permission-operation-card"
                      :class="{ granted: isFormPermissionGranted(operation.key) }"
                      :aria-pressed="isFormPermissionGranted(operation.key)"
                      :title="operation.description"
                      @click="
                        toggleFormPermission(
                          operation.key,
                          !isFormPermissionGranted(operation.key)
                        )
                      "
                    >
                      <span class="permission-operation-check editable" aria-hidden="true">
                        <Check v-if="isFormPermissionGranted(operation.key)" :size="9" />
                      </span>
                      <span>{{ operation.name }}</span>
                      <code>{{ permissionShortKey(operation.key) }}</code>
                    </button>
                  </div>
                </article>
              </div>
            </section>
          </div>
        </a-form-item>
      </a-form>
    </a-modal>
  </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { message, Modal } from 'ant-design-vue'
import {
  ArrowLeft,
  Ban,
  Check,
  ChevronRight,
  Copy,
  LockKeyhole,
  Plus,
  Search,
  SquarePen
} from 'lucide-vue-next'
import { useUserStore } from '@/stores/user'
import { getDepartments } from '@/apis/department_api'
import { copyRole, createRole, deactivateRole, getRoleOverview, updateRole } from '@/apis/role_api'
import FallbackAvatar from '@/components/common/FallbackAvatar.vue'
import {
  buildDepartmentTree,
  getDepartmentSelectionSummary,
  normalizeDepartmentSelection
} from '@/utils/departmentTree'
import {
  getDataScopeLabel,
  groupRolePermissions,
  serializeRoleMembersCsv,
  updateRolePermissionSelection
} from '@/utils/roleOverview'

const overview = ref({ permissions: [], data_scope_types: [], scope_departments: [], roles: [] })
const userStore = useUserStore()
const departments = ref([])
const selectedRoleId = ref(null)
const viewMode = ref('list')
const detailTab = ref('permissions')
const roleSearch = ref('')
const roleKind = ref('all')
const memberSearch = ref('')
const memberPage = ref(1)
const showGrantedOnly = ref(false)
const inlineEditing = ref(false)
const inlinePermissionKeys = ref([])
const loading = ref(false)
const saving = ref(false)
const errorMessage = ref('')
const editorOpen = ref(false)
const editorMode = ref('create')
const roleForm = reactive({
  code: '',
  name: '',
  description: '',
  permission_keys: [],
  default_scope_type: 'none',
  default_department_ids: []
})

const roleKindFilters = [
  { value: 'all', label: '全部' },
  { value: 'builtin', label: '内置' },
  { value: 'custom', label: '自定义' }
]
const memberPageSize = 8

const selectedRole = computed(
  () => overview.value.roles.find((role) => role.id === selectedRoleId.value) || null
)
const menuPermissionCatalog = computed(() =>
  overview.value.permissions.filter((permission) => !permission.parent_key)
)
const operationPermissionCatalog = computed(() =>
  overview.value.permissions.filter((permission) => permission.parent_key)
)
const filteredRoles = computed(() => {
  const query = roleSearch.value.trim().toLowerCase()
  return overview.value.roles.filter((role) => {
    const matchesKind =
      roleKind.value === 'all' ||
      (roleKind.value === 'builtin' ? role.is_builtin : !role.is_builtin)
    return (
      matchesKind &&
      (!query || role.name.toLowerCase().includes(query) || role.code.toLowerCase().includes(query))
    )
  })
})
const selectedPermissionKeys = computed(() => new Set(selectedRole.value?.permission_keys || []))
const displayPermissionGroups = computed(() =>
  groupRolePermissions(
    overview.value.permissions,
    inlineEditing.value
      ? inlinePermissionKeys.value
      : selectedRole.value?.permission_keys || [],
    showGrantedOnly.value && !inlineEditing.value
  )
)
const formPermissionGroups = computed(() =>
  groupRolePermissions(overview.value.permissions, roleForm.permission_keys)
)
const filteredMembers = computed(() => {
  const query = memberSearch.value.trim().toLowerCase()
  if (!query) return selectedRole.value?.members || []

  return (selectedRole.value?.members || []).filter(
    (member) =>
      member.username.toLowerCase().includes(query) || member.uid.toLowerCase().includes(query)
  )
})
const pagedMembers = computed(() => {
  const start = (memberPage.value - 1) * memberPageSize
  return filteredMembers.value.slice(start, start + memberPageSize)
})
const scopeOptions = computed(() =>
  overview.value.data_scope_types.map((scope) => ({ value: scope.key, label: scope.label }))
)
const departmentTree = computed(() => buildDepartmentTree(departments.value))
const scopeDepartments = computed(() =>
  departments.value.length ? departments.value : overview.value.scope_departments
)
const selectedScopeSummary = computed(() => getRoleScopeSummary(selectedRole.value))
const editorTitle = computed(
  () => ({ create: '创建角色', copy: '复制角色', edit: '编辑角色' })[editorMode.value]
)
const canManageRoles = computed(() => userStore.hasPermission('role:manage'))

/** 统计已选择的菜单或操作权限数量。 */
const countRolePermissions = (permissionKeys, operations) => {
  const selectedKeys = new Set(permissionKeys)
  const catalog = operations ? operationPermissionCatalog.value : menuPermissionCatalog.value
  return catalog.filter((permission) => selectedKeys.has(permission.key)).length
}

/** 返回权限标识中冒号后的短名称。 */
const permissionShortKey = (permissionKey) => permissionKey.split(':').at(-1)

/**
 * 返回角色默认数据范围的用户可读摘要。
 */
const getRoleScopeSummary = (role) => {
  if (!role) return ''
  if (role.default_scope_type === 'selected_organizations_and_descendants') {
    return getDepartmentSelectionSummary(scopeDepartments.value, role.default_department_ids)
  }
  return getDataScopeLabel(overview.value.data_scope_types, role.default_scope_type)
}

const loadOverview = async (preferredRoleId = selectedRoleId.value) => {
  loading.value = true
  errorMessage.value = ''
  try {
    overview.value = await getRoleOverview()
    selectedRoleId.value = overview.value.roles.some((role) => role.id === preferredRoleId)
      ? preferredRoleId
      : (overview.value.roles[0]?.id ?? null)
  } catch (error) {
    errorMessage.value = error.message || '角色与权限加载失败'
  } finally {
    loading.value = false
  }
}

const loadDepartments = async () => {
  if (departments.value.length) return true

  try {
    departments.value = await getDepartments()
    return true
  } catch (error) {
    message.error(error.message || '组织信息加载失败')
    return false
  }
}

/** 打开指定角色详情并重置详情筛选状态。 */
const openRole = (roleId) => {
  selectedRoleId.value = roleId
  viewMode.value = 'detail'
  detailTab.value = 'permissions'
  showGrantedOnly.value = false
  inlineEditing.value = false
  memberSearch.value = ''
}

/** 在存在未保存权限时确认是否放弃修改。 */
const confirmDiscardPermissionChanges = () =>
  !inlineEditing.value || window.confirm('权限修改尚未保存，确定放弃修改吗？')

/** 返回角色总览。 */
const backToList = () => {
  if (!confirmDiscardPermissionChanges()) return
  viewMode.value = 'list'
  inlineEditing.value = false
}

/** 打开成员页签。 */
const openMembersTab = () => {
  if (!confirmDiscardPermissionChanges()) return
  detailTab.value = 'members'
  inlineEditing.value = false
  memberPage.value = 1
}

/** 判断权限在当前查看或编辑状态下是否已勾选。 */
const isPermissionGranted = (permissionKey) =>
  inlineEditing.value
    ? inlinePermissionKeys.value.includes(permissionKey)
    : selectedPermissionKeys.value.has(permissionKey)

/** 进入页内权限编辑状态。 */
const startPermissionEdit = () => {
  inlinePermissionKeys.value = [...selectedRole.value.permission_keys]
  inlineEditing.value = true
  showGrantedOnly.value = false
}

/** 放弃页内权限修改。 */
const cancelPermissionEdit = () => {
  inlineEditing.value = false
  inlinePermissionKeys.value = []
}

/** 更新页内权限勾选值。 */
const toggleInlinePermission = (permissionKey, checked) => {
  inlinePermissionKeys.value = updateRolePermissionSelection(
    inlinePermissionKeys.value,
    overview.value.permissions,
    permissionKey,
    checked
  )
}

/** 判断角色表单中的权限是否已勾选。 */
const isFormPermissionGranted = (permissionKey) => roleForm.permission_keys.includes(permissionKey)

/** 更新角色表单权限并保持菜单与操作父子联动。 */
const toggleFormPermission = (permissionKey, checked) => {
  roleForm.permission_keys = updateRolePermissionSelection(
    roleForm.permission_keys,
    overview.value.permissions,
    permissionKey,
    checked
  )
}

/**
 * 下载当前筛选结果中的角色成员。
 */
const exportMembers = () => {
  const blob = new Blob([`\ufeff${serializeRoleMembersCsv(filteredMembers.value)}`], {
    type: 'text/csv;charset=utf-8'
  })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `${selectedRole.value.code}-members.csv`
  link.click()
  URL.revokeObjectURL(url)
}

/** 保存页内权限修改并刷新角色详情。 */
const savePermissions = async () => {
  saving.value = true
  try {
    const role = selectedRole.value
    const saved = await updateRole(role.id, {
      name: role.name,
      description: role.description,
      permission_keys: inlinePermissionKeys.value,
      default_scope_type: role.default_scope_type,
      default_department_ids: role.default_department_ids
    })
    inlineEditing.value = false
    await loadOverview(saved.id)
    message.success('角色权限已保存')
  } catch (error) {
    message.error(error.message || '角色权限保存失败')
  } finally {
    saving.value = false
  }
}

const fillRoleForm = (role = null) => {
  roleForm.code = role?.code || ''
  roleForm.name = role?.name || ''
  roleForm.description = role?.description || ''
  roleForm.permission_keys = [...(role?.permission_keys || [])]
  roleForm.default_scope_type = role?.default_scope_type || 'none'
  roleForm.default_department_ids = [...(role?.default_department_ids || [])]
}

const openCreate = () => {
  editorMode.value = 'create'
  fillRoleForm()
  editorOpen.value = true
}

const openCopy = () => {
  editorMode.value = 'copy'
  fillRoleForm(selectedRole.value)
  roleForm.code = `${selectedRole.value.code}_copy`
  roleForm.name = `${selectedRole.value.name}副本`
  editorOpen.value = true
}

const openEdit = async () => {
  if (
    selectedRole.value.default_scope_type === 'selected_organizations_and_descendants' &&
    !(await loadDepartments())
  ) {
    return
  }
  editorMode.value = 'edit'
  fillRoleForm(selectedRole.value)
  editorOpen.value = true
}

const handleScopeChange = async (scopeType) => {
  if (scopeType === 'selected_organizations_and_descendants') {
    if (!(await loadDepartments())) roleForm.default_scope_type = 'none'
    return
  }

  roleForm.default_department_ids = []
}

const saveRole = async () => {
  const codePattern = /^[a-z][a-z0-9_-]+$/
  if (!roleForm.name.trim()) return message.error('请输入角色名称')
  if (editorMode.value !== 'edit' && !codePattern.test(roleForm.code)) {
    return message.error('角色标识需以小写字母开头，仅包含小写字母、数字、下划线或连字符')
  }

  const departmentIds = normalizeDepartmentSelection(
    departments.value,
    roleForm.default_department_ids
  )
  if (
    editorMode.value !== 'copy' &&
    roleForm.default_scope_type === 'selected_organizations_and_descendants' &&
    !departmentIds.length
  ) {
    return message.error('请至少选择一个组织节点')
  }

  saving.value = true
  try {
    let saved
    if (editorMode.value === 'copy') {
      saved = await copyRole(selectedRole.value.id, {
        code: roleForm.code,
        name: roleForm.name.trim(),
        description: roleForm.description.trim()
      })
    } else {
      const definition = {
        name: roleForm.name.trim(),
        description: roleForm.description.trim(),
        permission_keys: roleForm.permission_keys,
        default_scope_type: roleForm.default_scope_type,
        default_department_ids: departmentIds
      }
      saved =
        editorMode.value === 'create'
          ? await createRole({ code: roleForm.code, ...definition })
          : await updateRole(selectedRole.value.id, definition)
    }

    editorOpen.value = false
    await loadOverview(saved.id)
    openRole(saved.id)
    message.success('角色已保存')
  } catch (error) {
    message.error(error.message || '角色保存失败')
  } finally {
    saving.value = false
  }
}

const confirmDeactivate = () => {
  Modal.confirm({
    title: `停用“${selectedRole.value.name}”？`,
    content: '停用后不能再分配该角色；如仍有成员，系统会要求先迁移成员。',
    okText: '停用',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      try {
        const role = await deactivateRole(selectedRole.value.id)
        await loadOverview(role.id)
        message.success('角色已停用')
      } catch (error) {
        message.error(error.message || '角色停用失败')
        throw error
      }
    }
  })
}

onMounted(() => {
  loadOverview()
  if (canManageRoles.value) loadDepartments()
})

onBeforeRouteLeave(() => confirmDiscardPermissionChanges())
</script>

<style scoped lang="less">
.role-management {
  color: var(--gray-800);
}

.role-load-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  border: 1px solid var(--color-error-100);
  border-radius: 8px;
  background: var(--color-error-50);
  color: var(--color-error-700);
}

.role-list-header,
.role-detail-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;

  h1,
  p {
    margin: 0;
  }

  h1 {
    color: var(--gray-900);
    font-size: 24px;
    font-weight: 650;
    line-height: 1.35;
  }

  p {
    max-width: 78ch;
    margin-top: 7px;
    color: var(--gray-600);
    font-size: 13.5px;
    line-height: 1.6;
  }

  :deep(.ant-btn) {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
}

.role-filter-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 24px;
}

.role-search {
  width: 260px;
}

.role-kind-filter,
.permission-filter {
  display: flex;
  gap: 3px;
  padding: 3px;
  border-radius: 8px;
  background: var(--gray-100);

  button {
    padding: 5px 12px;
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: var(--gray-500);
    font-size: 12.5px;
    cursor: pointer;

    &.active {
      background: var(--gray-0);
      color: var(--gray-900);
    }

    &:focus-visible {
      outline: 2px solid var(--main-400);
      outline-offset: 1px;
    }
  }
}

.role-list-summary {
  margin-left: auto;
  color: var(--gray-500);
  font-size: 12.5px;
  white-space: nowrap;
}

.role-table-scroll {
  margin-top: 14px;
  overflow-x: auto;
  border: 1px solid var(--gray-150);
  border-radius: 12px;
  background: var(--gray-0);
}

.role-table {
  min-width: 900px;
}

.role-table-header,
.role-table-row {
  display: grid;
  grid-template-columns: minmax(210px, 2fr) minmax(130px, 1.15fr) 92px 92px 72px 82px 66px;
  gap: 12px;
  align-items: center;
}

.role-table-header {
  padding: 11px 20px;
  border-bottom: 1px solid var(--gray-150);
  background: var(--gray-25);
  color: var(--gray-500);
  font-size: 11.5px;
}

.role-table-row {
  width: 100%;
  padding: 14px 20px;
  border: 0;
  border-bottom: 1px solid var(--gray-100);
  background: transparent;
  color: var(--gray-700);
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;

  &:last-child {
    border-bottom: 0;
  }

  &:hover {
    background: var(--gray-25);
  }

  &:focus-visible {
    outline: 2px solid var(--main-400);
    outline-offset: -3px;
  }
}

.role-identity {
  min-width: 0;

  code {
    display: block;
    margin-top: 3px;
    overflow: hidden;
    color: var(--gray-500);
    font-size: 11.5px;
    text-overflow: ellipsis;
  }
}

.role-name-line,
.role-title-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
}

.role-name-line strong {
  color: var(--gray-900);
  font-size: 14px;
  font-weight: 600;
}

.role-kind,
.role-state {
  display: inline-flex;
  padding: 2px 7px;
  border-radius: 5px;
  font-size: 11px;
  line-height: 1.35;
}

.role-kind.builtin {
  background: var(--color-info-50);
  color: var(--color-info-700);
}

.role-kind.custom {
  background: var(--main-50);
  color: var(--main-700);
}

.role-state {
  background: var(--color-success-50);
  color: var(--color-success-700);

  &.disabled {
    background: var(--gray-100);
    color: var(--gray-500);
  }
}

.role-view-link {
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 2px;
  color: var(--main-700);
}

.role-breadcrumb {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 14px;
  color: var(--gray-500);
  font-size: 12.5px;

  button {
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--main-700);
    cursor: pointer;

    &:focus-visible {
      outline: 2px solid var(--main-400);
      outline-offset: 2px;
    }
  }
}

.role-detail-copy {
  min-width: 0;
}

.role-title-row {
  h1 {
    font-size: 22px;
  }

  code {
    color: var(--gray-500);
    font-size: 12px;
  }
}

.role-summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 1px;
  margin-top: 20px;
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 10px;
  background: var(--gray-150);

  > div {
    padding: 14px 16px;
    background: var(--gray-0);
  }

  span,
  strong {
    display: block;
  }

  span {
    color: var(--gray-500);
    font-size: 11.5px;
  }

  strong {
    margin-top: 5px;
    color: var(--gray-900);
    font-size: 15px;
  }
}

.role-detail-tabs {
  display: flex;
  gap: 2px;
  margin-top: 22px;
  border-bottom: 1px solid var(--gray-150);

  button {
    margin-bottom: -1px;
    padding: 10px 14px;
    border: 0;
    border-bottom: 2px solid transparent;
    background: transparent;
    color: var(--gray-500);
    font-size: 13.5px;
    cursor: pointer;

    &.active {
      border-bottom-color: var(--main-700);
      color: var(--main-700);
      font-weight: 600;
    }

    &:focus-visible {
      outline: 2px solid var(--main-400);
      outline-offset: -3px;
    }
  }
}

.role-panel {
  margin-top: 18px;
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 12px;
  background: var(--gray-0);
}

.role-panel-header,
.member-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--gray-100);

  p {
    margin: 0;
    color: var(--gray-600);
    font-size: 13px;
  }

  small {
    display: block;
    margin-top: 4px;
    color: var(--gray-500);
    font-size: 11.5px;
  }
}

.permission-panel-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;

  :deep(.ant-btn) {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
}

.permission-legend {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-top: 5px;
  color: var(--gray-500);
  font-size: 11.5px;

  span {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }

  i {
    width: 9px;
    height: 9px;
    background: var(--main-700);

    &.menu {
      border-radius: 3px;
    }

    &.operation {
      border-radius: 50%;
      background: var(--color-warning-700);
    }
  }
}

.permission-locked {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 12px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-50);
  color: var(--gray-500);
  font-size: 13px;
}

.permission-edit-count {
  color: var(--gray-500);
  font-size: 12.5px;
}

.permission-groups {
  display: grid;
  gap: 22px;
  padding: 18px 20px 22px;
}

.permission-group-title {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  color: var(--gray-500);
  font-size: 12px;
  font-weight: 600;

  i {
    flex: 1;
    height: 1px;
    background: var(--gray-100);
  }
}

.permission-menu-list {
  display: grid;
  gap: 8px;
}

.permission-menu-card {
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 10px;
  background: var(--gray-25);

  &.granted {
    border-color: var(--main-100);
    background: var(--main-10);
  }
}

.permission-menu-header {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 11px 14px;
  border: 0;
  background: transparent;
  color: var(--gray-500);
  text-align: left;
  cursor: pointer;

  &:disabled {
    color: var(--gray-500);
    cursor: default;
  }

  .granted & {
    color: var(--gray-900);
  }

  strong {
    font-size: 13.5px;
    font-weight: 600;
  }

  code {
    color: var(--gray-400);
    font-size: 11px;
    white-space: nowrap;
  }

  &:focus-visible {
    outline: 2px solid var(--main-400);
    outline-offset: -2px;
  }
}

.permission-kind {
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--gray-100);
  color: var(--gray-500);
  font-size: 10.5px;
}

.permission-operation-summary {
  margin-left: auto;
  color: var(--gray-500);
  font-size: 11.5px;
  white-space: nowrap;
}

.permission-check {
  display: inline-flex;
  width: 16px;
  height: 16px;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--gray-300);
  border-radius: 50%;
  background: var(--gray-0);

  &.editable {
    border-radius: 4px;
  }

  .granted & {
    border-color: var(--main-700);
    background: var(--main-700);
    color: var(--gray-0);
  }
}

.permission-operation-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(212px, 1fr));
  gap: 6px;
  padding: 2px 14px 12px 40px;
}

.permission-operation-card {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  padding: 7px 10px;
  border: 1px solid var(--gray-100);
  border-radius: 7px;
  background: var(--gray-25);
  color: var(--gray-500);
  text-align: left;
  cursor: pointer;

  &.granted {
    border-color: var(--color-warning-100);
    background: var(--color-warning-10);
    color: var(--gray-900);
  }

  &:disabled {
    cursor: default;
  }

  > span:nth-child(2) {
    min-width: 0;
    overflow: hidden;
    font-size: 12.5px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  code {
    margin-left: auto;
    color: var(--gray-400);
    font-size: 10px;
    white-space: nowrap;
  }

  &:focus-visible {
    outline: 2px solid var(--color-warning-500);
    outline-offset: 1px;
  }
}

.permission-operation-check {
  display: inline-flex;
  width: 14px;
  height: 14px;
  flex: none;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--gray-300);
  border-radius: 50%;
  background: var(--gray-0);

  &.editable {
    border-radius: 4px;
  }

  .granted & {
    border-color: var(--color-warning-700);
    background: var(--color-warning-700);
    color: var(--gray-0);
  }
}

.permission-empty-operations {
  padding: 0 14px 11px 40px;
  color: var(--gray-400);
  font-size: 11.5px;
}

.member-toolbar {
  .ant-input-affix-wrapper {
    width: 280px;
  }
}

.member-toolbar-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-left: auto;

  > span {
    color: var(--gray-500);
    font-size: 12.5px;
  }
}

.member-table-header,
.member-row {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(180px, 1fr);
  gap: 14px;
  align-items: center;
  padding: 11px 20px;
}

.member-table-header {
  border-bottom: 1px solid var(--gray-150);
  background: var(--gray-25);
  color: var(--gray-500);
  font-size: 11.5px;
}

.member-row {
  border-bottom: 1px solid var(--gray-100);

  &:last-child {
    border-bottom: 0;
  }

  code {
    color: var(--gray-500);
    font-size: 12px;
  }
}

.member-identity {
  display: flex;
  align-items: center;
  gap: 10px;

  strong {
    color: var(--gray-800);
    font-size: 13.5px;
    font-weight: 550;
  }
}

.member-pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 20px;
  border-top: 1px solid var(--gray-100);

  > span {
    color: var(--gray-500);
    font-size: 12.5px;
  }
}

.role-form-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.permission-editor {
  display: grid;
  width: 100%;
  gap: 18px;
}

.permission-editor-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--gray-500);
  font-size: 12px;
}

.permission-editor-hint {
  margin: -12px 0 0;
  color: var(--gray-500);
  font-size: 11.5px;
}

@media (max-width: 760px) {
  .role-list-header,
  .role-detail-header,
  .role-panel-header {
    flex-direction: column;
  }

  .role-filter-bar {
    align-items: stretch;
    flex-wrap: wrap;
  }

  .role-search {
    width: 100%;
  }

  .role-list-summary {
    width: 100%;
    margin-left: 0;
  }

  .role-summary-grid,
  .role-form-row {
    grid-template-columns: 1fr;
  }

  .role-detail-tabs {
    overflow-x: auto;

    button {
      flex-shrink: 0;
    }
  }

  .permission-panel-actions {
    width: 100%;
    margin-left: 0;
  }

  .permission-operation-grid {
    grid-template-columns: 1fr;
  }

  .member-toolbar {
    align-items: flex-start;
    flex-direction: column;

    .ant-input-affix-wrapper {
      width: 100%;
    }
  }

  .member-toolbar-actions {
    width: 100%;
    margin-left: 0;
  }

  .member-table {
    overflow-x: auto;
  }

  .member-table-header,
  .member-row {
    min-width: 520px;
  }
}
</style>
