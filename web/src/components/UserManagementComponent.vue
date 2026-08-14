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
      </div>
    </div>

    <!-- 主内容区域 -->
    <div class="content-section">
      <a-spin :spinning="userManagement.loading">
        <div v-if="userManagement.error" class="error-message">
          <a-alert type="error" :message="userManagement.error" show-icon />
        </div>

        <div class="cards-container">
          <div v-if="filteredUsers.length === 0" class="empty-state">
            <a-empty
              :description="userManagement.users.length === 0 ? '暂无用户数据' : '没有匹配的用户'"
            />
          </div>
          <div v-else class="user-cards-grid">
            <InfoCard
              v-for="user in paginatedUsers"
              :key="user.id"
              :title="user.username"
              :subtitle="`ID: ${user.uid || '-'}`"
              class="user-card"
            >
              <template #icon>
                <FallbackAvatar
                  :src="user.avatar"
                  :default-src="getUserDefaultAvatarSrc(user)"
                  :name="user.username"
                  :seed="user.uid || user.username"
                  kind="user"
                  :size="40"
                  shape="circle"
                  :alt="user.username"
                  class="avatar-img"
                />
              </template>

              <template #status>
                <div
                  v-if="user.role === 'admin' || user.role === 'superadmin' || user.department_name"
                  class="role-dept-badge"
                >
                  <span class="role-icon-wrapper" :class="getRoleClass(user.role)">
                    <UserLock v-if="user.role === 'superadmin'" :size="14" />
                    <UserStar v-else-if="user.role === 'admin'" :size="14" />
                    <User v-else :size="14" />
                  </span>
                  <span v-if="user.department_name" class="dept-text">
                    {{ user.department_name }}
                  </span>
                </div>
              </template>

              <template v-if="canUpdateUsers || canDeleteUsers" #card-more-action-corner>
                <a-menu>
                  <a-menu-item v-if="canUpdateUsers" key="edit" @click.stop="showEditUserModal(user)">
                    <span class="lucide-menu-item">
                      <SquarePen :size="14" />
                      <span>编辑用户</span>
                    </span>
                  </a-menu-item>
                  <a-menu-item
                    v-if="canDeleteUsers"
                    key="delete"
                    :disabled="isUserDeleteDisabled(user)"
                    :danger="!isUserDeleteDisabled(user)"
                    @click.stop="confirmDeleteUser(user)"
                  >
                    <span class="lucide-menu-item">
                      <Trash2 :size="14" />
                      <span>删除用户</span>
                    </span>
                  </a-menu-item>
                </a-menu>
              </template>

              <template #info>
                <div class="card-content">
                  <div class="info-item">
                    <span class="info-label">角色:</span>
                    <span class="info-value">{{ getUserRoleNames(user) }}</span>
                  </div>
                  <div class="info-item">
                    <span class="info-label">手机号:</span>
                    <span class="info-value phone-text">{{ user.phone_number || '-' }}</span>
                  </div>
                  <div class="info-item">
                    <span class="info-label">创建时间:</span>
                    <span class="info-value time-text">{{ formatTime(user.created_at) }}</span>
                  </div>
                  <div class="info-item">
                    <span class="info-label">最后登录:</span>
                    <span class="info-value time-text">{{ formatTime(user.last_login) }}</span>
                  </div>
                </div>
              </template>
            </InfoCard>
          </div>
          <div v-if="filteredUsers.length > userManagement.pageSize" class="pagination-section">
            <a-pagination
              v-model:current="userManagement.currentPage"
              v-model:page-size="userManagement.pageSize"
              :total="filteredUsers.length"
              :page-size-options="['20', '50', '100']"
              show-size-changer
              size="small"
            />
          </div>
        </div>
      </a-spin>
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
          />
          <div v-if="userManagement.form.phoneError" class="error-text">
            {{ userManagement.form.phoneError }}
          </div>
        </a-form-item>

        <template v-if="userManagement.editMode">
          <div class="password-toggle">
            <a-checkbox v-model:checked="userManagement.displayPasswordFields">
              修改密码
            </a-checkbox>
          </div>
        </template>

        <template v-if="!userManagement.editMode || userManagement.displayPasswordFields">
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

        <a-form-item
          v-if="!userStore.isSuperAdmin && !userManagement.editMode"
          label="角色"
          class="form-item"
        >
          <a-select v-model:value="userManagement.form.role">
            <a-select-option value="user">普通用户</a-select-option>
          </a-select>
        </a-form-item>

        <a-form-item v-if="userStore.isSuperAdmin" label="角色分配" required class="form-item">
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
                  <a-radio value="inherit">继承角色默认范围</a-radio>
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
import {
  Plus,
  SquarePen,
  Trash2,
  User,
  UserLock,
  UserStar,
  RefreshCw,
  Search
} from 'lucide-vue-next'
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
import InfoCard from '@/components/shared/InfoCard.vue'

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
  pageSize: 50,
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
    role: 'user', // 默认角色
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
const roleOverview = reactive({ roles: [], dataScopeTypes: [] })

const treeFieldNames = { children: 'children', label: 'name', value: 'id' }
const departmentTree = computed(() => buildDepartmentTree(departmentManagement.departments))
const canCreateUsers = computed(() => userStore.hasPermission('user:create'))
const canUpdateUsers = computed(() => userStore.hasPermission('user:update'))
const canDeleteUsers = computed(() => userStore.hasPermission('user:delete'))
const canChooseUserDepartment = computed(() =>
  userManagement.editMode ? canUpdateUsers.value : canCreateUsers.value
)
const activeRoles = computed(() => roleOverview.roles.filter((role) => role.is_active))
const roleFilterOptions = computed(() =>
  activeRoles.value.length
    ? activeRoles.value
    : [
        { code: 'superadmin', name: '超级管理员' },
        { code: 'admin', name: '管理员' },
        { code: 'user', name: '普通用户' }
      ]
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

const paginatedUsers = computed(() => {
  const pageSize = Number(userManagement.pageSize)
  const start = (userManagement.currentPage - 1) * pageSize
  return filteredUsers.value.slice(start, start + pageSize)
})

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

const fetchRoleOptions = async (force = false) => {
  if (!userStore.isSuperAdmin || (!force && roleOverview.roles.length)) return

  try {
    const overview = await getRoleOverview()
    roleOverview.roles = overview.roles
    roleOverview.dataScopeTypes = overview.data_scope_types
  } catch (error) {
    console.error('获取角色列表失败:', error)
    message.error(error.message || '获取角色列表失败')
  }
}

const getRoleById = (roleId) => roleOverview.roles.find((role) => role.id === roleId)
const getScopeLabel = (scopeType) =>
  roleOverview.dataScopeTypes.find((scope) => scope.key === scopeType)?.label || scopeType
const getUserRoleNames = (user) =>
  (user.roles || []).map((role) => role.name).join('、') || user.role || '-'

const makeRoleAssignment = (role, existing = null) => ({
  role_id: role?.id ?? existing?.id ?? null,
  scope_mode: existing?.scope_mode || 'inherit',
  override_scope_type: existing?.override_scope_type || null,
  override_department_ids: [...(existing?.override_department_ids || [])]
})

const getDefaultRoleAssignments = () => {
  const userRole = roleOverview.roles.find((role) => role.code === 'user')
  return userRole ? [makeRoleAssignment(userRole)] : []
}

const getOverrideScopeOptions = (roleId) => {
  const role = getRoleById(roleId)
  return getAssignableScopeTypes(
    role?.default_scope_type,
    roleOverview.dataScopeTypes,
    departmentManagement.departments,
    userManagement.form.departmentId,
    role?.default_department_ids
  ).map((scope) => ({ value: scope.key, label: scope.label }))
}

const getOverrideDepartmentTree = (roleId) => {
  const role = getRoleById(roleId)
  let allowedRootIds = null
  if (role?.default_scope_type === 'organization_and_descendants') {
    allowedRootIds = userManagement.form.departmentId ? [userManagement.form.departmentId] : []
  } else if (role?.default_scope_type === 'selected_organizations_and_descendants') {
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
  if (role) userManagement.form.roleAssignments.push(makeRoleAssignment(role))
}

const removeRoleAssignment = (index) => {
  userManagement.form.roleAssignments.splice(index, 1)
}

const handleRoleChange = (index) => {
  const assignment = userManagement.form.roleAssignments[index]
  const isSuperadmin = getRoleById(assignment.role_id)?.code === 'superadmin'
  userManagement.form.roleAssignments = resetRoleAssignmentScope(
    userManagement.form.roleAssignments,
    index,
    isSuperadmin
  )
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
  user.id === userStore.userId ||
  (user.role === 'superadmin' && userStore.userRole !== 'superadmin')

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
    await Promise.all([fetchUsers(), fetchDepartments(), fetchRoleOptions(true)])
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
    role: 'user', // 默认角色为普通用户
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
  await Promise.all([fetchDepartments(), fetchRoleOptions()])
  userManagement.modalTitle = '编辑用户'
  userManagement.editMode = true
  userManagement.editUserId = user.id
  userManagement.originalHadSuperadmin = (user.roles || []).some(
    (role) => role.code === 'superadmin'
  )
  userManagement.form = {
    username: user.username,
    generatedUid: user.uid || '', // 编辑模式显示现有的uid
    phoneNumber: user.phone_number || '',
    password: '',
    confirmPassword: '',
    role: user.role,
    roleAssignments: (user.roles || []).map((role) => makeRoleAssignment(null, role)),
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

    if (userStore.isSuperAdmin) {
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
      const updateData = {
        username: userManagement.form.username.trim()
      }

      // 添加手机号字段
      if (userManagement.form.phoneNumber) {
        updateData.phone_number = userManagement.form.phoneNumber
      }

      if (canUpdateUsers.value && userManagement.form.departmentId) {
        updateData.department_id = userManagement.form.departmentId
      }

      if (userStore.isSuperAdmin) {
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

      if (userStore.isSuperAdmin) {
        createData.role_assignments = roleAssignments
        if (requiresSuperadminReason.value) createData.reason = userManagement.form.reason.trim()
      } else {
        createData.role = userManagement.form.role
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

const getRoleClass = (role) => {
  switch (role) {
    case 'superadmin':
      return 'role-superadmin'
    case 'admin':
      return 'role-admin'
    case 'user':
      return 'role-user'
    default:
      return 'role-default'
  }
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

      .filter-select {
        flex: 1;
        min-width: 0;
      }
    }
  }

  .content-section {
    overflow: hidden;

    .error-message {
      padding: 16px 24px;
    }

    .cards-container {
      .empty-state {
        padding: 60px 20px;
        text-align: center;
      }

      .user-cards-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
        gap: 16px;
        // padding: 16px;

        .user-card {
          cursor: default;

          :deep(.info-card-icon) {
            border-radius: 50%;
          }

          :deep(.info-card-body) {
            display: flex;
            flex-direction: column;
            gap: 8px;
          }

          .avatar-img {
            width: 100%;
            height: 100%;
            object-fit: cover;
          }

          .role-dept-badge {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 2px 8px 2px 4px;
            background: var(--gray-50);
            border-radius: 4px;

            .role-icon-wrapper {
              display: flex;
              align-items: center;
              justify-content: center;
              width: 16px;
              height: 16px;

              &.role-superadmin {
                color: var(--color-error-700);
              }
              &.role-admin {
                color: var(--color-info-700);
              }
              &.role-user {
                color: var(--color-success-700);
              }
            }

            .dept-text {
              font-size: 12px;
              color: var(--gray-700);
              font-weight: 500;
            }
          }

          .card-content {
            .info-item {
              display: flex;
              justify-content: space-between;
              align-items: center;
              padding: 2px 0;
              border-bottom: 1px solid var(--gray-25);

              &:last-child {
                border-bottom: none;
              }

              .info-label {
                font-size: 12px;
                color: var(--gray-600);
                font-weight: 500;
                min-width: 70px;
              }

              .info-value {
                font-size: 12px;
                color: var(--gray-900);
                text-align: right;
                flex: 1;

                &.time-text {
                  color: var(--gray-700);
                }

                &.phone-text {
                  font-family: 'Monaco', 'Consolas', monospace;
                }
              }
            }
          }
        }
      }

      .pagination-section {
        display: flex;
        justify-content: flex-end;
        margin-top: 16px;
      }
    }
  }

  .time-text {
    font-size: 13px;
    color: var(--gray-700);
  }

  .phone-text,
  .user-id-text {
    font-size: 13px;
    color: var(--gray-900);
    font-family: 'Monaco', 'Consolas', monospace;
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
