<template>
  <div class="user-management">
    <!-- 头部区域 -->
    <div class="header-section">
      <div class="header-content">
        <div class="section-title">用户管理</div>
        <p class="section-description">
          管理系统用户，请谨慎操作。删除用户后该用户将无法登录系统。
        </p>
      </div>
      <div class="header-actions">
        <a-button
          @click="handleRefresh"
          :loading="userManagement.refreshing"
          title="刷新"
          class="refresh-btn lucide-icon-btn"
        >
          <template #icon>
            <RefreshCw :size="16" :class="{ spin: userManagement.refreshing }" />
          </template>
        </a-button>
        <a-button
          v-if="canCreateUsers"
          type="primary"
          @click="showAddUserModal"
          class="add-btn lucide-icon-btn"
        >
          <template #icon><Plus :size="16" /></template>
          添加用户
        </a-button>
      </div>
    </div>

    <div class="filter-section">
      <a-input
        v-model:value="userManagement.searchKeyword"
        class="search-input"
        placeholder="搜索用户名 / ID / 手机号"
        allow-clear
      >
        <template #prefix><Search :size="16" /></template>
      </a-input>
      <div class="filter-actions">
        <a-tree-select
          v-model:value="userManagement.departmentFilter"
          :tree-data="departmentTree"
          :field-names="treeFieldNames"
          class="filter-select"
          placeholder="全部组织机构"
          tree-node-filter-prop="name"
          tree-default-expand-all
          show-search
          allow-clear
          :dropdown-style="{ maxHeight: '360px', overflow: 'auto' }"
        />
        <a-select v-model:value="userManagement.roleFilter" class="filter-select">
          <a-select-option value="">全部角色</a-select-option>
          <a-select-option v-for="role in roleFilterOptions" :key="role.code" :value="role.code">
            {{ role.name }}
          </a-select-option>
        </a-select>
        <a-button v-if="hasActiveFilters" @click="resetFilters">重置</a-button>
      </div>
      <span class="filter-summary">
        共 {{ filteredUsers.length }} 名用户 · 全部 {{ userManagement.users.length }} 名
      </span>
    </div>

    <!-- 主内容区域 -->
    <div class="content-section">
      <div v-if="userManagement.error" class="error-message">
        <a-alert type="error" :message="userManagement.error" show-icon />
      </div>

      <a-table
        :columns="userTableColumns"
        :data-source="filteredUsers"
        :loading="userManagement.loading"
        :pagination="tablePagination"
        :scroll="{ x: 920 }"
        row-key="id"
        size="middle"
        class="user-table"
        @change="handleTableChange"
      >
        <template #bodyCell="{ column, record: user }">
          <template v-if="column.key === 'user'">
            <div class="user-identity">
              <FallbackAvatar
                :src="user.avatar"
                :default-src="getUserDefaultAvatarSrc(user)"
                :name="user.username"
                :seed="user.uid || user.username"
                kind="user"
                :size="32"
                shape="circle"
                :alt="user.username"
              />
              <div class="user-identity-copy">
                <strong :title="user.username">{{ user.username }}</strong>
                <code :title="`登录 ID：${user.uid || '-'}`">登录 ID：{{ user.uid || '-' }}</code>
              </div>
            </div>
          </template>

          <template v-else-if="column.key === 'department'">
            <span class="department-text" :title="user.department_name || '未分配组织机构'">
              {{ user.department_name || '未分配' }}
            </span>
          </template>

          <template v-else-if="column.key === 'roles'">
            <div v-if="user.roles?.length" class="role-tags">
              <span
                v-for="role in user.roles"
                :key="role.assignment_id || role.id"
                class="role-tag"
                :class="{ inactive: !role.is_active }"
              >
                {{ role.name }}
              </span>
            </div>
            <span v-else class="empty-value">-</span>
          </template>

          <template v-else-if="column.key === 'phone'">
            <code class="phone-text">{{ user.phone_number || '-' }}</code>
          </template>

          <template v-else-if="column.key === 'created'">
            <div class="time-cell">
              <span>{{ formatTime(user.created_at) }}</span>
              <small>最后登录 {{ formatTime(user.last_login) }}</small>
            </div>
          </template>

          <template v-else-if="column.key === 'actions'">
            <div class="row-actions">
              <a-button
                v-if="canUpdateUsers || canAssignRoles"
                type="text"
                size="small"
                @click="showEditUserModal(user)"
              >
                编辑
              </a-button>
              <a-dropdown v-if="canDeleteUsers" :trigger="['click']">
                <a-button
                  size="small"
                  class="more-action lucide-icon-btn"
                  :aria-label="`更多操作：${user.username}`"
                >
                  <MoreHorizontal :size="16" />
                </a-button>
                <template #overlay>
                  <a-menu>
                    <a-menu-item
                      key="delete"
                      :disabled="isUserDeleteDisabled(user)"
                      :danger="!isUserDeleteDisabled(user)"
                      @click="confirmDeleteUser(user)"
                    >
                      <span class="lucide-menu-item">
                        <Trash2 :size="14" />
                        <span>删除用户</span>
                      </span>
                    </a-menu-item>
                  </a-menu>
                </template>
              </a-dropdown>
            </div>
          </template>
        </template>

        <template #emptyText>
          <a-empty
            :description="
              userManagement.users.length === 0
                ? canCreateUsers
                  ? '暂无用户数据，可使用右上角“添加用户”创建'
                  : '暂无用户数据'
                : '没有匹配的用户，请调整筛选条件'
            "
          />
        </template>
      </a-table>
    </div>

    <!-- 用户表单模态框 -->
    <a-modal
      v-model:open="userManagement.modalVisible"
      :title="userManagement.modalTitle"
      @ok="handleUserFormSubmit"
      :confirmLoading="userManagement.loading"
      @cancel="userManagement.modalVisible = false"
      :maskClosable="false"
      width="720px"
      class="user-modal"
    >
      <a-form layout="vertical" class="user-form">
        <a-form-item label="用户名" required class="form-item">
          <a-input
            v-model:value="userManagement.form.username"
            placeholder="请输入用户名（2-20个字符）"
            @blur="validateAndGenerateUid"
            :maxlength="20"
            :disabled="userManagement.editMode && !canUpdateUsers"
          />
          <div v-if="userManagement.form.usernameError" class="error-text">
            {{ userManagement.form.usernameError }}
          </div>
          <div
            v-if="userManagement.form.generatedUid && !userManagement.editMode"
            class="help-text"
          >
            登录ID：{{ userManagement.form.generatedUid }}，此ID将用于登录，根据用户名自动生成
          </div>
        </a-form-item>

        <!-- 手机号字段 -->
        <a-form-item label="手机号" class="form-item">
          <a-input
            v-model:value="userManagement.form.phoneNumber"
            placeholder="请输入手机号（可选，可用于登录）"
            :maxlength="11"
            :disabled="userManagement.editMode && !canUpdateUsers"
          />
          <div v-if="userManagement.form.phoneError" class="error-text">
            {{ userManagement.form.phoneError }}
          </div>
        </a-form-item>

        <template v-if="userManagement.editMode && canUpdateUsers">
          <div class="password-toggle">
            <a-checkbox v-model:checked="userManagement.displayPasswordFields">
              修改密码
            </a-checkbox>
          </div>
        </template>

        <template
          v-if="
            !userManagement.editMode || (canUpdateUsers && userManagement.displayPasswordFields)
          "
        >
          <a-form-item label="密码" required class="form-item">
            <a-input-password
              v-model:value="userManagement.form.password"
              :placeholder="`请输入密码（至少 ${MIN_PASSWORD_LENGTH} 位）`"
              :minlength="MIN_PASSWORD_LENGTH"
            />
          </a-form-item>

          <a-form-item label="确认密码" required class="form-item">
            <a-input-password
              v-model:value="userManagement.form.confirmPassword"
              placeholder="请再次输入密码"
            />
          </a-form-item>
        </template>

        <a-form-item v-if="canEditRoleAssignments" label="角色分配" required class="form-item">
          <div class="role-assignment-list">
            <section
              v-for="(assignment, index) in userManagement.form.roleAssignments"
              :key="index"
              class="role-assignment"
            >
              <div class="role-assignment-header">
                <a-select
                  v-model:value="assignment.role_id"
                  placeholder="请选择角色"
                  style="flex: 1"
                  @change="handleRoleChange(index)"
                >
                  <a-select-option
                    v-for="role in activeRoles"
                    :key="role.id"
                    :value="role.id"
                    :disabled="isRoleSelectedByOther(role.id, index)"
                  >
                    {{ role.name }}{{ role.is_builtin ? '（内置）' : '' }}
                  </a-select-option>
                </a-select>
                <a-button
                  danger
                  :disabled="userManagement.form.roleAssignments.length === 1"
                  @click="removeRoleAssignment(index)"
                >
                  移除
                </a-button>
              </div>

              <template v-if="getRoleById(assignment.role_id)">
                <p class="role-default-scope">
                  默认范围：{{ getScopeLabel(getRoleById(assignment.role_id).default_scope_type) }}
                </p>
                <a-radio-group
                  v-model:value="assignment.scope_mode"
                  @change="handleScopeModeChange(assignment)"
                >
                  <a-radio
                    value="inherit"
                    :disabled="
                      getRoleById(assignment.role_id)?.assignment_constraints?.can_inherit === false
                    "
                  >
                    继承角色默认范围
                  </a-radio>
                  <a-radio value="override">个性化收窄</a-radio>
                </a-radio-group>

                <div v-if="assignment.scope_mode === 'override'" class="role-override-fields">
                  <a-select
                    v-model:value="assignment.override_scope_type"
                    :options="getOverrideScopeOptions(assignment.role_id)"
                    placeholder="请选择更窄的数据范围"
                    @change="handleOverrideScopeChange(assignment)"
                  />
                  <a-tree-select
                    v-if="
                      assignment.override_scope_type === 'selected_organizations_and_descendants'
                    "
                    v-model:value="assignment.override_department_ids"
                    :tree-data="getOverrideDepartmentTree(assignment.role_id)"
                    :field-names="treeFieldNames"
                    tree-checkable
                    tree-default-expand-all
                    allow-clear
                    placeholder="请选择组织子树"
                  />
                </div>
              </template>
            </section>

            <a-button v-if="canAddRoleAssignment" block @click="addRoleAssignment">
              添加角色
            </a-button>
          </div>
        </a-form-item>

        <a-form-item v-if="requiresSuperadminReason" label="变更原因" required class="form-item">
          <a-textarea
            v-model:value="userManagement.form.reason"
            :maxlength="500"
            :rows="2"
            placeholder="请说明授予或撤销超级管理员的原因"
          />
        </a-form-item>

        <a-form-item v-if="canChooseUserDepartment" label="所属组织机构" class="form-item">
          <a-tree-select
            v-model:value="userManagement.form.departmentId"
            :tree-data="departmentTree"
            :field-names="treeFieldNames"
            placeholder="请选择组织机构"
            tree-node-filter-prop="name"
            tree-default-expand-all
            show-search
            :dropdown-style="{ maxHeight: '360px', overflow: 'auto' }"
            @change="handleUserDepartmentChange"
          />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup>
import { reactive, onMounted, watch, computed } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { useUserStore } from '@/stores/user'
import { departmentApi } from '@/apis'
import { getRoleOverview } from '@/apis/role_api'
import { MoreHorizontal, Plus, Trash2, RefreshCw, Search } from 'lucide-vue-next'
import { formatDateTime } from '@/utils/time'
import { isPasswordLongEnough, MIN_PASSWORD_LENGTH } from '@/utils/passwordValidation'
import { generatePixelAvatar } from '@/utils/pixelAvatar'
import {
  buildDepartmentScopeTree,
  buildDepartmentTree,
  isDepartmentSelectionCovered,
  normalizeDepartmentSelection
} from '@/utils/departmentTree'
import { getAssignableScopeTypes, resetRoleAssignmentScope } from '@/utils/roleOverview'
import FallbackAvatar from '@/components/common/FallbackAvatar.vue'

const userStore = useUserStore()

// 用户管理相关状态
const userManagement = reactive({
  loading: false,
  refreshing: false,
  users: [],
  searchKeyword: '',
  departmentFilter: null,
  roleFilter: '',
  currentPage: 1,
  pageSize: 10,
  error: null,
  modalVisible: false,
  modalTitle: '添加用户',
  editMode: false,
  editUserId: null,
  originalHadSuperadmin: false,
  form: {
    username: '',
    generatedUid: '', // 自动生成的uid
    phoneNumber: '', // 手机号
    password: '',
    confirmPassword: '',
    roleAssignments: [],
    reason: '',
    departmentId: null, // 部门ID
    usernameError: '', // 用户名错误信息
    phoneError: '' // 手机号错误信息
  },
  displayPasswordFields: true // 编辑时是否显示密码字段
})

// 组织机构列表
const departmentManagement = reactive({
  departments: []
})
const roleOverview = reactive({ roles: [], dataScopeTypes: [], targetUserId: null })

const treeFieldNames = { children: 'children', label: 'name', value: 'id' }
const userTableColumnsWithActions = [
  { title: '用户', key: 'user', width: 210 },
  { title: '组织机构', key: 'department', width: 140 },
  { title: '角色', key: 'roles', width: 150 },
  { title: '手机号', key: 'phone', width: 130 },
  { title: '创建时间', key: 'created', width: 180 },
  { title: '', key: 'actions', width: 100, align: 'right' }
]
const departmentTree = computed(() => buildDepartmentTree(departmentManagement.departments))
const canCreateUsers = computed(() => userStore.hasPermission('user:create'))
const canUpdateUsers = computed(() => userStore.hasPermission('user:update'))
const canAssignRoles = computed(() => userStore.hasPermission('user:role_assign'))
const canDeleteUsers = computed(() => userStore.hasPermission('user:delete'))
const userTableColumns = computed(() =>
  canUpdateUsers.value || canAssignRoles.value || canDeleteUsers.value
    ? userTableColumnsWithActions
    : userTableColumnsWithActions.slice(0, -1)
)
const canEditRoleAssignments = computed(
  () => canAssignRoles.value && (userManagement.editMode || userStore.hasPermission('role:read'))
)
const canChooseUserDepartment = computed(() =>
  userManagement.editMode ? canUpdateUsers.value : canCreateUsers.value
)
const activeRoles = computed(() => roleOverview.roles.filter((role) => role.is_active))
const roleFilterOptions = computed(() =>
  activeRoles.value.length
    ? activeRoles.value
    : Array.from(
        new Map(
          userManagement.users.flatMap((user) => user.roles || []).map((role) => [role.code, role])
        ).values()
      )
)
const hasSuperadminAssignment = computed(() =>
  userManagement.form.roleAssignments.some(
    (assignment) => getRoleById(assignment.role_id)?.code === 'superadmin'
  )
)
const requiresSuperadminReason = computed(
  () => hasSuperadminAssignment.value !== userManagement.originalHadSuperadmin
)
const canAddRoleAssignment = computed(
  () =>
    !hasSuperadminAssignment.value &&
    userManagement.form.roleAssignments.length < activeRoles.value.length
)
const hasActiveFilters = computed(
  () =>
    Boolean(userManagement.searchKeyword.trim()) ||
    userManagement.departmentFilter != null ||
    Boolean(userManagement.roleFilter)
)

const filteredUsers = computed(() => {
  const keyword = userManagement.searchKeyword.trim().toLowerCase()

  return userManagement.users.filter((user) => {
    const matchesKeyword =
      !keyword ||
      [user.username, user.uid, user.phone_number].some((value) =>
        String(value || '')
          .toLowerCase()
          .includes(keyword)
      )
    const matchesDepartment =
      userManagement.departmentFilter == null ||
      isDepartmentSelectionCovered(
        departmentManagement.departments,
        [userManagement.departmentFilter],
        [user.department_id]
      )
    const matchesRole =
      !userManagement.roleFilter ||
      (user.roles || []).some((role) => role.code === userManagement.roleFilter)

    return matchesKeyword && matchesDepartment && matchesRole
  })
})

const tablePagination = computed(() => ({
  current: userManagement.currentPage,
  pageSize: Number(userManagement.pageSize),
  total: filteredUsers.value.length,
  pageSizeOptions: ['10', '20', '50'],
  showSizeChanger: true,
  showTotal: (total, range) => `第 ${range[0]}–${range[1]} 条，共 ${total} 条`
}))

/** 清空筛选后由现有 watch 统一将表格退回第一页。 */
const resetFilters = () => {
  userManagement.searchKeyword = ''
  userManagement.departmentFilter = null
  userManagement.roleFilter = ''
}

/** 保持表格分页受控，以便筛选和数据刷新时能校正当前页。 */
const handleTableChange = (pagination) => {
  userManagement.currentPage = pagination.current
  userManagement.pageSize = pagination.pageSize
}

// 获取组织机构列表
const fetchDepartments = async () => {
  departmentManagement.departments = []
  try {
    departmentManagement.departments = await departmentApi.getDepartments()
  } catch (error) {
    message.error(error.message || '获取组织机构列表失败')
    throw error
  }
}

const fetchRoleOptions = async (force = false, targetUserId = null) => {
  if (targetUserId == null && !userStore.hasPermission('role:read')) return
  if (targetUserId != null && !canAssignRoles.value) return
  if (!force && roleOverview.roles.length && roleOverview.targetUserId === targetUserId) return

  try {
    const overview = await getRoleOverview(targetUserId)
    roleOverview.roles = overview.roles
    roleOverview.dataScopeTypes = overview.data_scope_types
    roleOverview.targetUserId = targetUserId
  } catch (error) {
    console.error('获取角色列表失败:', error)
    message.error(error.message || '获取角色列表失败')
    throw error
  }
}

const getRoleById = (roleId) => roleOverview.roles.find((role) => role.id === roleId)
const getScopeLabel = (scopeType) =>
  roleOverview.dataScopeTypes.find((scope) => scope.key === scopeType)?.label || scopeType
const makeRoleAssignment = (role, existing = null) => ({
  role_id: role?.id ?? existing?.id ?? null,
  scope_mode: existing?.scope_mode || 'inherit',
  override_scope_type: existing?.override_scope_type || null,
  override_department_ids: [...(existing?.override_department_ids || [])]
})

const getDefaultRoleAssignments = () => {
  const defaultRole = roleOverview.roles.find((role) => role.code === 'user')
  return defaultRole ? [makeRoleAssignment(defaultRole)] : []
}

const getOverrideScopeOptions = (roleId) => {
  const role = getRoleById(roleId)
  const scopes = getAssignableScopeTypes(
    role?.default_scope_type,
    roleOverview.dataScopeTypes,
    departmentManagement.departments,
    userManagement.form.departmentId,
    role?.default_department_ids,
    role?.assignment_constraints
  )
  return scopes.map((scope) => ({
    value: scope.key,
    label: scope.label
  }))
}

const getOverrideDepartmentTree = (roleId) => {
  const role = getRoleById(roleId)
  let allowedRootIds = role?.assignment_constraints
    ? normalizeDepartmentSelection(
        departmentManagement.departments,
        role.assignment_constraints.override_department_ids
      )
    : null
  if (allowedRootIds === null && role?.default_scope_type === 'organization_and_descendants') {
    allowedRootIds = userManagement.form.departmentId ? [userManagement.form.departmentId] : []
  } else if (
    allowedRootIds === null &&
    role?.default_scope_type === 'selected_organizations_and_descendants'
  ) {
    allowedRootIds = role.default_department_ids
  }
  return buildDepartmentScopeTree(departmentManagement.departments, allowedRootIds)
}

const isRoleSelectedByOther = (roleId, currentIndex) =>
  userManagement.form.roleAssignments.some(
    (assignment, index) => index !== currentIndex && assignment.role_id === roleId
  )

const addRoleAssignment = () => {
  const selected = new Set(userManagement.form.roleAssignments.map((item) => item.role_id))
  const role = activeRoles.value.find(
    (item) => !selected.has(item.id) && item.code !== 'superadmin'
  )
  if (role) {
    userManagement.form.roleAssignments.push(makeRoleAssignment(role))
    handleRoleChange(userManagement.form.roleAssignments.length - 1)
  }
}

const removeRoleAssignment = (index) => {
  userManagement.form.roleAssignments.splice(index, 1)
}

const handleRoleChange = (index) => {
  const assignment = userManagement.form.roleAssignments[index]
  const role = getRoleById(assignment.role_id)
  const isSuperadmin = role?.code === 'superadmin'
  userManagement.form.roleAssignments = resetRoleAssignmentScope(
    userManagement.form.roleAssignments,
    index,
    isSuperadmin
  )
  const changed = userManagement.form.roleAssignments[isSuperadmin ? 0 : index]
  if (role?.assignment_constraints?.can_inherit === false) {
    changed.scope_mode = 'override'
    changed.override_scope_type = getOverrideScopeOptions(role.id)[0]?.value || null
  }
}

const handleScopeModeChange = (assignment) => {
  if (assignment.scope_mode === 'inherit') {
    assignment.override_scope_type = null
    assignment.override_department_ids = []
    return
  }

  assignment.override_scope_type = getOverrideScopeOptions(assignment.role_id)[0]?.value || null
}

const handleOverrideScopeChange = (assignment) => {
  if (assignment.override_scope_type !== 'selected_organizations_and_descendants') {
    assignment.override_department_ids = []
  }
}

const handleUserDepartmentChange = () => {
  for (const assignment of userManagement.form.roleAssignments) {
    const defaultScopeType = getRoleById(assignment.role_id)?.default_scope_type
    if (
      assignment.scope_mode === 'override' &&
      ['organization_and_descendants', 'selected_organizations_and_descendants'].includes(
        defaultScopeType
      )
    ) {
      assignment.scope_mode = 'inherit'
      assignment.override_scope_type = null
      assignment.override_department_ids = []
    }
  }
}

// 添加验证用户名并生成uid的函数
const validateAndGenerateUid = async () => {
  const username = userManagement.form.username.trim()

  // 清空之前的错误和生成的ID
  userManagement.form.usernameError = ''
  userManagement.form.generatedUid = ''

  if (!username) {
    return
  }

  // 在编辑模式下，不需要重新生成uid
  if (userManagement.editMode) {
    return
  }

  try {
    const result = await userStore.validateUsernameAndGenerateUid(username)
    userManagement.form.generatedUid = result.uid
  } catch (error) {
    userManagement.form.usernameError = error.message || '用户名验证失败'
  }
}

// 验证手机号格式
const validatePhoneNumber = (phone) => {
  if (!phone) {
    return true // 手机号可选
  }

  // 中国大陆手机号格式验证
  const phoneRegex = /^1[3-9]\d{9}$/
  return phoneRegex.test(phone)
}

// 监听密码字段显示状态变化
watch(
  () => userManagement.displayPasswordFields,
  (newVal) => {
    // 当取消显示密码字段时，清空密码输入
    if (!newVal) {
      userManagement.form.password = ''
      userManagement.form.confirmPassword = ''
    }
  }
)

// 监听手机号输入变化
watch(
  () => userManagement.form.phoneNumber,
  (newPhone) => {
    userManagement.form.phoneError = ''

    if (newPhone && !validatePhoneNumber(newPhone)) {
      userManagement.form.phoneError = '请输入正确的手机号格式'
    }
  }
)

watch(
  () => [userManagement.searchKeyword, userManagement.departmentFilter, userManagement.roleFilter],
  () => {
    userManagement.currentPage = 1
  }
)

watch(
  () => filteredUsers.value.length,
  (total) => {
    const maxPage = Math.max(1, Math.ceil(total / Number(userManagement.pageSize)))
    if (userManagement.currentPage > maxPage) {
      userManagement.currentPage = maxPage
    }
  }
)

// 格式化时间显示
const formatTime = (timeStr) => formatDateTime(timeStr)

const getUserDefaultAvatarSrc = (user) => (user.uid ? generatePixelAvatar(user.uid) : '')

const isUserDeleteDisabled = (user) =>
  user.id === userStore.userId || (user.roles || []).some((role) => role.code === 'superadmin')

// 获取用户列表
const fetchUsers = async () => {
  try {
    userManagement.loading = true
    const users = await userStore.getUsers()
    userManagement.users = users
    userManagement.error = null
  } catch (error) {
    console.error('获取用户列表失败:', error)
    userManagement.error = '获取用户列表失败'
  } finally {
    userManagement.loading = false
  }
}

// 刷新用户和部门信息
const handleRefresh = async () => {
  if (userManagement.refreshing) return
  userManagement.refreshing = true
  try {
    await Promise.all([
      fetchUsers(),
      fetchDepartments(),
      fetchRoleOptions(true, userManagement.editMode ? userManagement.editUserId : null)
    ])
    message.success('刷新成功')
  } catch (error) {
    console.error('刷新失败:', error)
  } finally {
    userManagement.refreshing = false
  }
}

// 打开添加用户模态框
const showAddUserModal = async () => {
  await Promise.all([fetchDepartments(), fetchRoleOptions()])
  userManagement.modalTitle = '添加用户'
  userManagement.editMode = false
  userManagement.editUserId = null
  userManagement.originalHadSuperadmin = false
  userManagement.form = {
    username: '',
    generatedUid: '',
    phoneNumber: '',
    password: '',
    confirmPassword: '',
    roleAssignments: getDefaultRoleAssignments(),
    reason: '',
    departmentId: userStore.departmentId,
    usernameError: '',
    phoneError: ''
  }
  userManagement.displayPasswordFields = true
  userManagement.modalVisible = true
}

// 打开编辑用户模态框
const showEditUserModal = async (user) => {
  userManagement.modalTitle = '编辑用户'
  userManagement.editMode = true
  userManagement.editUserId = user.id
  await Promise.all([fetchDepartments(), fetchRoleOptions(false, user.id)])
  userManagement.originalHadSuperadmin = (user.roles || []).some(
    (role) => role.code === 'superadmin'
  )
  const roleAssignments = (user.roles || [])
    .filter((role) => getRoleById(role.id))
    .map((role) => makeRoleAssignment(null, role))
  for (const assignment of roleAssignments) {
    const role = getRoleById(assignment.role_id)
    if (
      assignment.scope_mode === 'inherit' &&
      role?.assignment_constraints?.can_inherit === false
    ) {
      assignment.scope_mode = 'override'
      assignment.override_scope_type = getOverrideScopeOptions(role.id)[0]?.value || null
      assignment.override_department_ids = []
    }
  }
  userManagement.form = {
    username: user.username,
    generatedUid: user.uid || '', // 编辑模式显示现有的uid
    phoneNumber: user.phone_number || '',
    password: '',
    confirmPassword: '',
    roleAssignments,
    reason: '',
    departmentId: user.department_id || null,
    usernameError: '',
    phoneError: ''
  }
  userManagement.displayPasswordFields = false // 默认不显示密码字段
  userManagement.modalVisible = true
}

// 处理用户表单提交
const handleUserFormSubmit = async () => {
  try {
    // 简单验证
    if (!userManagement.form.username.trim()) {
      message.error('用户名不能为空')
      return
    }

    // 验证用户名长度
    if (
      userManagement.form.username.trim().length < 2 ||
      userManagement.form.username.trim().length > 20
    ) {
      message.error('用户名长度必须在 2-20 个字符之间')
      return
    }

    // 验证手机号
    if (userManagement.form.phoneNumber && !validatePhoneNumber(userManagement.form.phoneNumber)) {
      message.error('请输入正确的手机号格式')
      return
    }

    if (userManagement.displayPasswordFields) {
      if (!userManagement.form.password) {
        message.error('密码不能为空')
        return
      }

      if (!isPasswordLongEnough(userManagement.form.password)) {
        message.error(`密码至少需要 ${MIN_PASSWORD_LENGTH} 个字符`)
        return
      }

      if (userManagement.form.password !== userManagement.form.confirmPassword) {
        message.error('两次输入的密码不一致')
        return
      }
    }

    if (canEditRoleAssignments.value) {
      if (!userManagement.form.roleAssignments.length) {
        message.error('请至少分配一个角色')
        return
      }
      for (const assignment of userManagement.form.roleAssignments) {
        if (!assignment.role_id) {
          message.error('请选择角色')
          return
        }
        if (assignment.scope_mode === 'override' && !assignment.override_scope_type) {
          message.error('请选择个性化数据范围')
          return
        }
        if (
          assignment.override_scope_type === 'selected_organizations_and_descendants' &&
          !assignment.override_department_ids.length
        ) {
          message.error('指定组织及下级范围至少需要选择一个组织节点')
          return
        }
      }
      if (requiresSuperadminReason.value && !userManagement.form.reason.trim()) {
        message.error('授予或撤销超级管理员必须填写原因')
        return
      }
    }

    userManagement.loading = true

    const roleAssignments = userManagement.form.roleAssignments.map((assignment) => ({
      role_id: assignment.role_id,
      scope_mode: assignment.scope_mode,
      override_scope_type:
        assignment.scope_mode === 'override' ? assignment.override_scope_type : null,
      override_department_ids:
        assignment.scope_mode === 'override'
          ? normalizeDepartmentSelection(
              departmentManagement.departments,
              assignment.override_department_ids
            )
          : []
    }))

    // 根据模式决定创建还是更新用户
    if (userManagement.editMode) {
      // 创建更新数据对象
      const updateData = {}
      if (canUpdateUsers.value) {
        updateData.username = userManagement.form.username.trim()
        if (userManagement.form.phoneNumber) {
          updateData.phone_number = userManagement.form.phoneNumber
        }
        if (userManagement.form.departmentId) {
          updateData.department_id = userManagement.form.departmentId
        }
      }

      if (canEditRoleAssignments.value) {
        updateData.role_assignments = roleAssignments
        if (requiresSuperadminReason.value) updateData.reason = userManagement.form.reason.trim()
      }

      // 如果显示了密码字段并且填写了密码，才更新密码
      if (userManagement.displayPasswordFields && userManagement.form.password) {
        updateData.password = userManagement.form.password
      }

      await userStore.updateUser(userManagement.editUserId, updateData)
      message.success('用户更新成功')
    } else {
      // 创建新用户
      const createData = {
        username: userManagement.form.username.trim(),
        password: userManagement.form.password
      }

      if (canEditRoleAssignments.value) {
        createData.role_assignments = roleAssignments
        if (requiresSuperadminReason.value) createData.reason = userManagement.form.reason.trim()
      }

      if (canCreateUsers.value && userManagement.form.departmentId) {
        createData.department_id = userManagement.form.departmentId
      }

      // 添加手机号字段（如果填写了）
      if (userManagement.form.phoneNumber) {
        createData.phone_number = userManagement.form.phoneNumber
      }

      await userStore.createUser(createData)
      message.success('用户创建成功')
    }

    // 重新获取用户列表
    await fetchUsers()
    userManagement.modalVisible = false
  } catch (error) {
    console.error('用户操作失败:', error)
    message.error(error.message || '操作失败，请稍后重试')
  } finally {
    userManagement.loading = false
  }
}

// 删除用户
const confirmDeleteUser = (user) => {
  // 自己不能删除自己
  if (user.id === userStore.userId) {
    message.error('不能删除自己的账户')
    return
  }

  // 确认对话框
  Modal.confirm({
    title: '确认删除用户',
    content: `确定要删除用户 "${user.username}" 吗？此操作不可撤销。`,
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      try {
        userManagement.loading = true
        await userStore.deleteUser(user.id)
        message.success('用户删除成功')
        // 重新获取用户列表
        await fetchUsers()
      } catch (error) {
        console.error('删除用户失败:', error)
        message.error(error.message || '删除失败，请稍后重试')
      } finally {
        userManagement.loading = false
      }
    }
  })
}

// 在组件挂载时获取用户列表
onMounted(async () => {
  await Promise.all([fetchUsers(), fetchDepartments(), fetchRoleOptions()])
})
</script>

<style lang="less" scoped>
.user-management {
  .header-section {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 16px;
    margin-bottom: 16px;

    .header-content {
      flex: 1;
      min-width: 0;

      .section-title {
        font-size: 16px;
        font-weight: 500;
        color: var(--gray-900);
        line-height: 1.4;
        margin: 12px 0 12px;
      }

      .section-description {
        font-size: 14px;
        color: var(--gray-600);
        line-height: 1.4;
        margin: 0;
      }
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 8px;

      .refresh-btn {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 32px;
        height: 32px;
        border-radius: 6px;
        transition: all 0.2s ease;

        &:hover {
          background: var(--gray-25);
        }

        .spin {
          animation: spin 1s linear infinite;
        }

        :deep(.ant-btn-loading-icon) {
          color: var(--gray-600);
        }
      }
    }
  }

  .filter-section {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 16px;
    flex-wrap: wrap;

    .search-input {
      width: 300px;
      max-width: 100%;

      :deep(.ant-input-prefix) {
        color: var(--gray-500);
        margin-right: 6px;
      }
    }

    .filter-actions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      margin-left: auto;
    }

    .filter-select {
      width: 150px;
    }

    .filter-summary {
      margin-left: auto;
      color: var(--gray-500);
      font-size: 12.5px;
      white-space: nowrap;
    }
  }

  @media (max-width: 640px) {
    .filter-section {
      align-items: stretch;

      .search-input,
      .filter-actions {
        width: 100%;
      }

      .filter-actions {
        margin-left: 0;
      }

      .filter-summary {
        margin-left: 0;
      }

      .filter-select {
        flex: 1;
        min-width: 0;
      }
    }
  }

  .content-section {
    overflow-x: auto;
    border: 1px solid var(--gray-150);
    border-radius: 8px;
    background: var(--gray-0);

    .error-message {
      padding: 16px;
    }

    .user-table {
      min-width: 920px;

      :deep(.ant-table) {
        background: var(--gray-0);
      }

      :deep(.ant-table-thead > tr > th) {
        padding: 11px 16px;
        background: var(--gray-25);
        color: var(--gray-500);
        font-size: 12px;
        font-weight: 500;
      }

      :deep(.ant-table-tbody > tr > td) {
        padding: 12px 16px;
        color: var(--gray-700);
        border-bottom-color: var(--gray-100);
      }

      :deep(.ant-table-tbody > tr:hover > td) {
        background: var(--gray-25);
      }

      :deep(.ant-table-pagination.ant-pagination) {
        margin: 12px 16px;
      }
    }

    .user-identity {
      display: flex;
      align-items: center;
      min-width: 0;
      gap: 10px;
    }

    .user-identity-copy {
      display: flex;
      min-width: 0;
      flex-direction: column;
      line-height: 1.35;

      strong,
      code {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      strong {
        color: var(--gray-900);
        font-size: 13.5px;
      }

      code {
        color: var(--gray-500);
        font-size: 11.5px;
      }
    }

    .department-text {
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .role-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
    }

    .role-tag {
      padding: 2px 7px;
      border-radius: 5px;
      background: var(--gray-100);
      color: var(--gray-700);
      font-size: 11.5px;
      line-height: 18px;

      &.inactive {
        background: var(--gray-100);
        color: var(--gray-500);
        text-decoration: line-through;
      }
    }

    .empty-value {
      color: var(--gray-400);
    }

    .phone-text {
      color: var(--gray-700);
      font-size: 12.5px;
    }

    .time-cell {
      display: flex;
      flex-direction: column;
      color: var(--gray-600);
      font-size: 12.5px;
      line-height: 1.5;

      small {
        color: var(--gray-400);
        font-size: 11.5px;
      }
    }

    .row-actions {
      display: flex;
      justify-content: flex-end;
      gap: 6px;

      .more-action {
        width: 28px;
        padding: 0;
      }
    }
  }
}

@media (max-width: 640px) {
  .user-management .content-section .row-actions {
    :deep(.ant-btn) {
      min-height: 40px;
    }

    .more-action {
      width: 40px;
    }
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

.user-modal {
  :deep(.ant-modal-header) {
    padding: 20px 24px 16px;
    border-bottom: 1px solid var(--gray-150);

    .ant-modal-title {
      font-size: 17px;
      font-weight: 600;
      color: var(--gray-900);
    }
  }

  :deep(.ant-modal-body) {
    padding: 20px 24px 24px;
  }

  .user-form {
    .form-item {
      margin-bottom: 16px;

      :deep(.ant-form-item-label) {
        padding-bottom: 6px;

        label {
          font-weight: 600;
          font-size: 13px;
          color: var(--gray-800);
        }
      }
    }

    .error-text {
      color: var(--color-error-500);
      font-size: 12px;
      margin-top: 4px;
      line-height: 1.3;
    }

    .help-text {
      color: var(--gray-600);
      font-size: 12px;
      margin-top: 4px;
      line-height: 1.3;
    }

    .password-toggle {
      margin-bottom: 16px;
      padding: 12px 16px;
      background: var(--gray-25);
      border-radius: 8px;
      border: 1px solid var(--gray-100);

      :deep(.ant-checkbox-wrapper) {
        font-weight: 500;
        color: var(--gray-700);
        font-size: 13px;
      }
    }

    .role-assignment-list {
      display: grid;
      gap: 10px;
    }

    .role-assignment {
      padding: 12px;
      border: 1px solid var(--gray-150);
      border-radius: 8px;
      background: var(--gray-25);
    }

    .role-assignment-header,
    .role-override-fields {
      display: flex;
      gap: 8px;
    }

    .role-default-scope {
      margin: 8px 0;
      color: var(--gray-600);
      font-size: 12px;
    }

    .role-override-fields {
      margin-top: 10px;

      > * {
        flex: 1;
      }
    }
  }
}

@media (max-width: 640px) {
  .user-modal .user-form {
    .role-assignment-header,
    .role-override-fields {
      flex-direction: column;
    }
  }
}
</style>
