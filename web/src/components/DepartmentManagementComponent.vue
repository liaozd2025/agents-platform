<template>
  <div class="department-management">
    <!-- 头部区域 -->
    <div class="header-section">
      <div class="header-content">
        <div class="section-title">组织机构管理</div>
        <p class="section-description">按集团、分子公司和部门维护组织节点及其层级关系。</p>
      </div>
      <div class="header-actions">
        <a-button
          @click="handleRefresh"
          :loading="departmentManagement.refreshing"
          title="刷新"
          class="refresh-btn lucide-icon-btn"
        >
          <template #icon
            ><RefreshCw :size="16" :class="{ spin: departmentManagement.refreshing }"
          /></template>
        </a-button>
        <a-button
          v-if="canCreateDepartments"
          type="primary"
          @click="showAddDepartmentModal"
          class="add-btn lucide-icon-btn"
        >
          <template #icon><Plus :size="16" /></template>
          添加组织节点
        </a-button>
      </div>
    </div>

    <!-- 主内容区域 -->
    <div class="content-section">
      <a-spin :spinning="departmentManagement.loading">
        <div v-if="departmentManagement.error" class="error-message">
          <a-alert type="error" :message="departmentManagement.error" show-icon />
        </div>

        <template v-else-if="departmentManagement.departments.length > 0">
          <div class="tree-toolbar">
            <a-space>
              <a-button size="small" @click="expandAllDepartments">
                <template #icon><ChevronsDown :size="14" /></template>
                全部展开
              </a-button>
              <a-button size="small" @click="collapseAllDepartments">
                <template #icon><ChevronsUp :size="14" /></template>
                全部收起
              </a-button>
            </a-space>
          </div>
          <a-table
            :expanded-row-keys="departmentManagement.expandedRowKeys"
            :dataSource="departmentTree"
            :columns="columns"
            :rowKey="(record) => record.id"
            :pagination="false"
            :scroll="{ x: 720 }"
            @expand="handleDepartmentExpand"
            class="department-table"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'name'">
                <div class="department-name">
                  <span class="name-text">{{ record.name }}</span>
                </div>
              </template>
              <template v-if="column.key === 'nodeType'">
                <a-tag>{{ NODE_TYPE_LABELS[record.node_type] || record.node_type }}</a-tag>
              </template>
              <template v-if="column.key === 'description'">
                <span class="description-text">{{ record.description || '-' }}</span>
              </template>
              <template v-if="column.key === 'userCount'">
                <span>{{ record.user_count ?? 0 }} 人</span>
              </template>
              <template v-if="column.key === 'action'">
                <a-space>
                  <a-tooltip title="编辑组织节点">
                    <a-button
                      v-if="canUpdateDepartments"
                      type="text"
                      size="small"
                      @click="showEditDepartmentModal(record)"
                      class="action-btn lucide-icon-btn"
                    >
                      <SquarePen :size="14" />
                    </a-button>
                  </a-tooltip>
                  <a-tooltip :title="getDeleteDisabledReason(record) || '删除组织节点'">
                    <a-button
                      v-if="canDeleteDepartments"
                      type="text"
                      size="small"
                      danger
                      @click="confirmDeleteDepartment(record)"
                      :disabled="Boolean(getDeleteDisabledReason(record))"
                      class="action-btn lucide-icon-btn"
                    >
                      <Trash2 :size="14" />
                    </a-button>
                  </a-tooltip>
                </a-space>
              </template>
            </template>
          </a-table>
        </template>

        <div v-else class="empty-state">
          <a-empty description="暂无组织节点，请先添加组织节点" />
        </div>
      </a-spin>
    </div>

    <!-- 组织节点表单模态框 -->
    <a-modal
      v-model:open="departmentManagement.modalVisible"
      :title="departmentManagement.modalTitle"
      @ok="handleDepartmentFormSubmit"
      :confirmLoading="departmentManagement.loading"
      @cancel="departmentManagement.modalVisible = false"
      :maskClosable="false"
      width="560px"
      class="department-modal"
    >
      <a-form layout="vertical" class="department-form" autocomplete="off">
        <a-form-item label="组织节点名称" required class="form-item">
          <a-input
            v-model:value="departmentManagement.form.name"
            placeholder="请输入组织节点名称"
            size="large"
            :maxlength="50"
          />
        </a-form-item>

        <a-form-item
          v-if="
            !departmentManagement.editMode ||
            departmentManagement.editDepartmentId !== ROOT_DEPARTMENT_ID
          "
          label="父级组织节点"
          required
          class="form-item"
        >
          <a-tree-select
            v-model:value="departmentManagement.form.parentId"
            :tree-data="parentTreeData"
            :field-names="treeFieldNames"
            :tree-default-expanded-keys="[ROOT_DEPARTMENT_ID]"
            tree-node-filter-prop="name"
            show-search
            size="large"
            placeholder="请选择父级组织节点"
            :dropdown-style="{ maxHeight: '360px', overflow: 'auto' }"
          />
          <div v-if="departmentManagement.editMode" class="help-text">
            修改父级会连同该节点的整棵子树一起移动
          </div>
          <div v-else class="help-text">新建子节点会继承父节点已授权的资源</div>
        </a-form-item>

        <a-form-item
          v-if="!departmentManagement.editMode"
          label="节点类型"
          required
          class="form-item"
        >
          <a-select v-model:value="departmentManagement.form.nodeType" size="large">
            <a-select-option value="group">集团</a-select-option>
            <a-select-option value="company">分子公司</a-select-option>
            <a-select-option value="department">部门</a-select-option>
          </a-select>
          <div class="help-text">节点类型仅用于界面展示，不限制组织层级</div>
        </a-form-item>

        <a-form-item label="组织节点描述" class="form-item">
          <a-textarea
            v-model:value="departmentManagement.form.description"
            placeholder="请输入组织节点描述（可选）"
            :rows="3"
            :maxlength="255"
            show-count
          />
        </a-form-item>

        <a-divider v-if="!departmentManagement.editMode && canCreateDepartmentAdmin" />

        <template v-if="!departmentManagement.editMode && canCreateDepartmentAdmin">
          <a-alert
            class="admin-section-hint"
            type="warning"
            message="同节点管理员账号可选"
            description="填写后创建内置管理员，默认管理该节点及下级；全部留空可只创建组织节点。"
            show-icon
          />

          <a-form-item label="管理员 UID（可选）" class="form-item">
            <a-input
              v-model:value="departmentManagement.form.adminUid"
              placeholder="请输入管理员UID（3-20位字母/数字/下划线）"
              size="large"
              :maxlength="20"
              name="new-department-admin-uid"
              autocomplete="off"
              @input="departmentManagement.form.uidError = ''"
              @blur="checkAdminUid"
            />
            <div v-if="departmentManagement.form.uidError" class="error-text">
              {{ departmentManagement.form.uidError }}
            </div>
            <div v-else class="help-text">开始填写任一管理员字段后，需要完整填写 UID 和密码</div>
          </a-form-item>

          <a-form-item label="管理员密码（可选）" class="form-item">
            <a-input-password
              v-model:value="departmentManagement.form.adminPassword"
              :placeholder="`请输入管理员密码（至少 ${MIN_PASSWORD_LENGTH} 位）`"
              size="large"
              :minlength="MIN_PASSWORD_LENGTH"
              :maxlength="50"
              name="new-department-admin-password"
              autocomplete="new-password"
            />
          </a-form-item>

          <a-form-item label="确认管理员密码（可选）" class="form-item">
            <a-input-password
              v-model:value="departmentManagement.form.adminConfirmPassword"
              placeholder="请再次输入密码"
              size="large"
              :maxlength="50"
              name="new-department-admin-password-confirmation"
              autocomplete="new-password"
            />
          </a-form-item>

          <a-form-item label="手机号（可选）" class="form-item">
            <a-input
              v-model:value="departmentManagement.form.adminPhone"
              placeholder="请输入手机号（可用于登录）"
              size="large"
              :maxlength="11"
              name="new-department-admin-phone"
              autocomplete="off"
            />
            <div v-if="departmentManagement.form.phoneError" class="error-text">
              {{ departmentManagement.form.phoneError }}
            </div>
          </a-form-item>
        </template>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, watch } from 'vue'
import { notification, message, Modal } from 'ant-design-vue'
import { departmentApi } from '@/apis'
import { authApi } from '@/apis/auth_api'
import { ChevronsDown, ChevronsUp, Plus, RefreshCw, SquarePen, Trash2 } from 'lucide-vue-next'
import {
  buildDepartmentTree,
  getDepartmentExpandableKeys,
  updateDepartmentExpandedKeys
} from '@/utils/departmentTree'
import { isPasswordLongEnough, MIN_PASSWORD_LENGTH } from '@/utils/passwordValidation'
import { useUserStore } from '@/stores/user'

const ROOT_DEPARTMENT_ID = 1
const NODE_TYPE_LABELS = {
  group: '集团',
  company: '分子公司',
  department: '部门'
}
const treeFieldNames = { children: 'children', label: 'name', value: 'id' }
const userStore = useUserStore()
const canCreateDepartments = computed(() => userStore.hasPermission('department:create'))
const canUpdateDepartments = computed(() => userStore.hasPermission('department:update'))
const canDeleteDepartments = computed(() => userStore.hasPermission('department:delete'))
const canCreateDepartmentAdmin = computed(
  () => userStore.hasPermission('user:create') && userStore.hasPermission('user:role_assign')
)

/** 创建一份组织节点表单初始状态。 */
const emptyDepartmentForm = () => ({
  name: '',
  description: '',
  parentId: ROOT_DEPARTMENT_ID,
  nodeType: 'department',
  adminUid: '',
  adminPassword: '',
  adminConfirmPassword: '',
  adminPhone: '',
  uidError: '',
  phoneError: ''
})

const columns = [
  {
    title: '组织节点',
    dataIndex: 'name',
    key: 'name',
    width: 240
  },
  {
    title: '节点类型',
    dataIndex: 'node_type',
    key: 'nodeType',
    width: 110
  },
  {
    title: '描述',
    dataIndex: 'description',
    key: 'description',
    ellipsis: true
  },
  {
    title: '直属用户',
    dataIndex: 'user_count',
    key: 'userCount',
    width: 100,
    align: 'center'
  },
  {
    title: '操作',
    key: 'action',
    width: 120,
    align: 'center'
  }
]

const departmentManagement = reactive({
  loading: false,
  refreshing: false,
  departments: [],
  expandedRowKeys: [],
  error: null,
  modalVisible: false,
  modalTitle: '添加组织节点',
  editMode: false,
  editDepartmentId: null,
  form: emptyDepartmentForm()
})

const departmentTree = computed(() => buildDepartmentTree(departmentManagement.departments))

const expandAllDepartments = () => {
  departmentManagement.expandedRowKeys = getDepartmentExpandableKeys(
    departmentManagement.departments
  )
}

const collapseAllDepartments = () => {
  departmentManagement.expandedRowKeys = []
}

const handleDepartmentExpand = (expanded, department) => {
  departmentManagement.expandedRowKeys = updateDepartmentExpandedKeys(
    departmentManagement.expandedRowKeys,
    department.id,
    expanded
  )
}

const getDeleteDisabledReason = (department) => {
  if (department.id === ROOT_DEPARTMENT_ID) return '集团根不可删除'
  if (department.children?.length) return '该组织节点下还有子节点，请先处理子节点'
  if (department.user_count) return '该组织节点下还有直属用户，请先调整用户的组织归属'
  return ''
}

const parentTreeData = computed(() => {
  const disabledRootId =
    departmentManagement.editMode && departmentManagement.editDepartmentId !== ROOT_DEPARTMENT_ID
      ? departmentManagement.editDepartmentId
      : null

  return buildDepartmentTree(departmentManagement.departments, disabledRootId)
})

// 获取组织节点列表
const fetchDepartments = async () => {
  try {
    departmentManagement.loading = true
    departmentManagement.error = null
    const departments = await departmentApi.getDepartments()
    departmentManagement.departments = departments
    expandAllDepartments()
  } catch (error) {
    console.error('获取组织节点列表失败:', error)
    departmentManagement.error = error.message || '获取组织节点列表失败'
  } finally {
    departmentManagement.loading = false
  }
}

const handleRefresh = async () => {
  if (departmentManagement.refreshing) return
  departmentManagement.refreshing = true
  try {
    await fetchDepartments()
    message.success('刷新成功')
  } catch (error) {
    console.error('刷新失败:', error)
    message.error('刷新失败')
  } finally {
    departmentManagement.refreshing = false
  }
}

const showAddDepartmentModal = () => {
  departmentManagement.modalTitle = '添加组织节点'
  departmentManagement.editMode = false
  departmentManagement.editDepartmentId = null
  departmentManagement.form = emptyDepartmentForm()
  departmentManagement.modalVisible = true
}

const showEditDepartmentModal = (department) => {
  departmentManagement.modalTitle = '编辑组织节点'
  departmentManagement.editMode = true
  departmentManagement.editDepartmentId = department.id
  departmentManagement.form = {
    ...emptyDepartmentForm(),
    name: department.name,
    description: department.description || '',
    parentId: department.parent_id
  }
  departmentManagement.modalVisible = true
}

const validatePhoneNumber = (phone) => {
  if (!phone) {
    return true // 手机号可选
  }
  const phoneRegex = /^1[3-9]\d{9}$/
  return phoneRegex.test(phone)
}

watch(
  () => departmentManagement.form.adminPhone,
  (newPhone) => {
    departmentManagement.form.phoneError = ''
    if (newPhone && !validatePhoneNumber(newPhone)) {
      departmentManagement.form.phoneError = '请输入正确的手机号格式'
    }
  }
)

const checkAdminUid = async () => {
  const uid = departmentManagement.form.adminUid.trim()
  departmentManagement.form.uidError = ''

  if (!uid) {
    return
  }

  // 验证格式
  if (!/^[a-zA-Z0-9_]+$/.test(uid)) {
    departmentManagement.form.uidError = 'UID只能包含字母、数字和下划线'
    return
  }

  if (uid.length < 3 || uid.length > 20) {
    departmentManagement.form.uidError = 'UID长度必须在3-20个字符之间'
    return
  }

  // 检查是否已存在
  try {
    const result = await authApi.checkUidAvailability(uid)
    if (!result.is_available) {
      departmentManagement.form.uidError = '该UID已被使用'
    }
  } catch (error) {
    console.error('检查UID失败:', error)
  }
}

const handleDepartmentFormSubmit = async () => {
  try {
    if (!departmentManagement.form.name.trim()) {
      notification.error({ message: '组织节点名称不能为空' })
      return
    }

    if (departmentManagement.form.name.trim().length < 2) {
      notification.error({ message: '组织节点名称至少2个字符' })
      return
    }

    const parentRequired =
      !departmentManagement.editMode || departmentManagement.editDepartmentId !== ROOT_DEPARTMENT_ID
    if (parentRequired && departmentManagement.form.parentId == null) {
      notification.error({ message: '请选择父级组织节点' })
      return
    }

    const adminUid = departmentManagement.form.adminUid.trim()
    const shouldCreateAdmin = Boolean(
      adminUid ||
      departmentManagement.form.adminPassword ||
      departmentManagement.form.adminConfirmPassword ||
      departmentManagement.form.adminPhone
    )

    if (!departmentManagement.editMode && shouldCreateAdmin) {
      if (!adminUid) {
        notification.error({ message: '创建管理员时必须填写 UID' })
        return
      }

      if (!/^[a-zA-Z0-9_]+$/.test(adminUid)) {
        notification.error({ message: 'UID只能包含字母、数字和下划线' })
        return
      }

      if (adminUid.length < 3 || adminUid.length > 20) {
        notification.error({ message: 'UID长度必须在3-20个字符之间' })
        return
      }

      if (departmentManagement.form.uidError) {
        notification.error({ message: '管理员 UID 已存在或格式错误' })
        return
      }

      if (!departmentManagement.form.adminPassword) {
        notification.error({ message: '创建管理员时必须填写密码' })
        return
      }

      if (!isPasswordLongEnough(departmentManagement.form.adminPassword)) {
        notification.error({ message: `密码至少需要 ${MIN_PASSWORD_LENGTH} 个字符` })
        return
      }

      if (
        !departmentManagement.form.adminConfirmPassword ||
        departmentManagement.form.adminPassword !== departmentManagement.form.adminConfirmPassword
      ) {
        notification.error({ message: '两次输入的密码不一致' })
        return
      }

      if (
        departmentManagement.form.adminPhone &&
        !validatePhoneNumber(departmentManagement.form.adminPhone)
      ) {
        notification.error({ message: '请输入正确的手机号格式' })
        return
      }
    }

    departmentManagement.loading = true

    if (departmentManagement.editMode) {
      const payload = {
        name: departmentManagement.form.name.trim(),
        description: departmentManagement.form.description.trim() || undefined
      }
      if (departmentManagement.editDepartmentId !== ROOT_DEPARTMENT_ID) {
        payload.parent_id = departmentManagement.form.parentId
      }

      await departmentApi.updateDepartment(departmentManagement.editDepartmentId, payload)
      notification.success({ message: '组织节点更新成功' })
    } else {
      const payload = {
        name: departmentManagement.form.name.trim(),
        description: departmentManagement.form.description.trim() || undefined,
        parent_id: departmentManagement.form.parentId,
        node_type: departmentManagement.form.nodeType
      }
      if (shouldCreateAdmin) {
        Object.assign(payload, {
          admin_uid: adminUid,
          admin_password: departmentManagement.form.adminPassword,
          admin_phone: departmentManagement.form.adminPhone || undefined
        })
      }

      await departmentApi.createDepartment(payload)

      message.success(
        shouldCreateAdmin ? `组织节点创建成功，管理员 “${adminUid}” 已创建` : '组织节点创建成功'
      )
    }

    await fetchDepartments()
    departmentManagement.modalVisible = false
  } catch (error) {
    console.error('组织节点操作失败:', error)
    notification.error({
      message: '操作失败',
      description: error.message || '请稍后重试'
    })
  } finally {
    departmentManagement.loading = false
  }
}

const confirmDeleteDepartment = (department) => {
  Modal.confirm({
    title: '确认删除组织节点',
    content: `确定要删除组织节点 “${department.name}” 吗？此操作不可撤销，节点关联的 API Key 会一并清理。`,
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      try {
        departmentManagement.loading = true
        await departmentApi.deleteDepartment(department.id)
        notification.success({ message: '组织节点删除成功' })
        await fetchDepartments()
      } catch (error) {
        console.error('删除部门失败:', error)
        notification.error({
          message: '删除失败',
          description: error.message || '请稍后重试'
        })
      } finally {
        departmentManagement.loading = false
      }
    }
  })
}

onMounted(() => {
  fetchDepartments()
})
</script>

<style lang="less" scoped>
.department-management {
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
      }
    }
  }

  .content-section {
    overflow-x: auto;

    .error-message {
      padding: 16px 24px;
    }

    .empty-state {
      padding: 60px 20px;
      text-align: center;
    }

    .tree-toolbar {
      display: flex;
      justify-content: flex-end;
      margin-bottom: 8px;
    }

    .department-table {
      :deep(.ant-table-thead > tr > th) {
        background: var(--gray-50);
        font-weight: 500;
        padding: 8px 12px;
      }

      :deep(.ant-table-tbody > tr > td) {
        padding: 8px 12px;
      }

      .department-name {
        min-width: 0;

        .name-text {
          font-weight: 500;
          color: var(--gray-900);
        }
      }

      .description-text {
        color: var(--gray-600);
      }

      .action-btn {
        padding: 4px 8px;
        border-radius: 6px;
        transition: all 0.2s ease;

        &:hover {
          background: var(--gray-25);
        }
      }
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

.department-modal {
  :deep(.ant-modal-header) {
    padding: 20px 24px;
    border-bottom: 1px solid var(--gray-150);

    .ant-modal-title {
      font-size: 16px;
      font-weight: 600;
      color: var(--gray-900);
    }
  }

  :deep(.ant-modal-body) {
    padding: 24px;
    max-height: calc(100vh - 180px);
    overflow-y: auto;
  }

  .department-form {
    .admin-section-hint {
      margin-bottom: 20px;
    }

    .form-item {
      margin-bottom: 20px;

      :deep(.ant-form-item-label) {
        padding-bottom: 4px;

        label {
          font-weight: 500;
          color: var(--gray-900);
        }
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
}

@media (max-width: 768px) {
  .department-management .header-section {
    align-items: stretch;
    flex-direction: column;

    .header-actions {
      justify-content: flex-end;
    }
  }
}
</style>
