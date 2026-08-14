<template>
  <section class="role-management">
    <header class="role-toolbar">
      <div>
        <h2>角色与权限</h2>
        <p>内置角色受系统保护；自定义角色采用停用而非删除。</p>
      </div>
      <a-button v-if="canManageRoles" type="primary" @click="openCreate">创建角色</a-button>
    </header>

    <a-spin :spinning="loading">
      <div v-if="errorMessage" class="role-load-error" role="alert">
        <span>{{ errorMessage }}</span>
        <a-button size="small" @click="loadOverview()">重新加载</a-button>
      </div>

      <div v-else-if="overview.roles.length" class="role-workspace">
        <aside class="role-list" aria-label="角色列表">
          <button
            v-for="role in overview.roles"
            :key="role.id"
            type="button"
            class="role-list-item"
            :class="{ active: selectedRole?.id === role.id }"
            :aria-current="selectedRole?.id === role.id ? 'true' : undefined"
            @click="selectedRoleId = role.id"
          >
            <span class="role-list-name">{{ role.name }}</span>
            <span class="role-list-meta">
              <span>{{ role.member_count }} 人</span>
              <span>{{ role.is_builtin ? '内置' : role.is_active ? '自定义' : '已停用' }}</span>
            </span>
          </button>
        </aside>

        <article v-if="selectedRole" class="role-detail">
          <header class="role-detail-header">
            <div>
              <div class="role-title-row">
                <h2>{{ selectedRole.name }}</h2>
                <a-tag color="blue">{{
                  selectedRole.is_builtin ? '内置角色' : '自定义角色'
                }}</a-tag>
                <a-tag :color="selectedRole.is_active ? 'green' : 'default'">
                  {{ selectedRole.is_active ? '已启用' : '已停用' }}
                </a-tag>
              </div>
              <p>{{ selectedRole.description || '暂无说明' }}</p>
              <code>{{ selectedRole.code }}</code>
            </div>
            <a-space v-if="canManageRoles" wrap>
              <a-button @click="openCopy">复制</a-button>
              <a-button v-if="!selectedRole.is_builtin" @click="openEdit">编辑</a-button>
              <a-button
                v-if="!selectedRole.is_builtin && selectedRole.is_active"
                danger
                @click="confirmDeactivate"
              >
                停用
              </a-button>
            </a-space>
          </header>

          <div class="role-summary-grid">
            <div class="role-summary-card">
              <span>默认数据范围</span>
              <strong>{{ selectedScopeSummary }}</strong>
            </div>
            <div class="role-summary-card">
              <span>功能权限</span>
              <strong>{{ selectedRole.permission_keys.length }} 项</strong>
            </div>
            <div class="role-summary-card">
              <span>当前成员</span>
              <strong>{{ selectedRole.member_count }} 人</strong>
            </div>
          </div>

          <section class="role-detail-section">
            <h3>功能权限</h3>
            <div v-if="permissionGroups.length" class="permission-groups">
              <div v-for="group in permissionGroups" :key="group.label" class="permission-group">
                <div class="permission-group-title">{{ group.label }}</div>
                <div class="permission-list">
                  <div
                    v-for="permission in group.permissions"
                    :key="permission.key"
                    class="permission-item"
                  >
                    <span class="permission-name">{{ permission.name }}</span>
                    <span class="permission-description">{{ permission.description }}</span>
                    <code>{{ permission.key }}</code>
                  </div>
                </div>
              </div>
            </div>
            <a-empty v-else :image="null" description="暂无功能权限" />
          </section>

          <section class="role-detail-section">
            <h3>成员</h3>
            <div v-if="selectedRole.members.length" class="role-members">
              <div v-for="member in selectedRole.members" :key="member.id" class="role-member">
                <span>{{ member.username }}</span>
                <code>{{ member.uid }}</code>
              </div>
            </div>
            <a-empty v-else :image="null" description="暂无成员" />
          </section>

          <section class="role-detail-section">
            <h3>最近安全审计</h3>
            <div v-if="selectedRole.audits.length" class="role-audits">
              <article v-for="audit in selectedRole.audits" :key="audit.id" class="role-audit">
                <div class="role-audit-header">
                  <strong>{{ getRoleAuditActionLabel(audit.action) }}</strong>
                  <span>{{ formatAuditTime(audit.created_at) }}</span>
                </div>
                <p>{{ audit.actor.username }}（{{ audit.actor.uid }}）</p>
                <details>
                  <summary>查看变更前后值</summary>
                  <div class="audit-values">
                    <div>
                      <span>变更前</span>
                      <pre>{{ formatAuditValue(audit.before) }}</pre>
                    </div>
                    <div>
                      <span>变更后</span>
                      <pre>{{ formatAuditValue(audit.after) }}</pre>
                    </div>
                  </div>
                </details>
              </article>
            </div>
            <a-empty v-else :image="null" description="暂无相关审计" />
          </section>
        </article>
      </div>

      <a-empty v-else-if="!loading" description="暂无角色" />
    </a-spin>

    <a-modal
      v-model:open="editorOpen"
      :title="editorTitle"
      :confirm-loading="saving"
      width="720px"
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
          <a-textarea v-model:value="roleForm.description" :rows="2" :maxlength="2000" />
        </a-form-item>

        <a-alert
          v-if="editorMode === 'copy'"
          type="info"
          show-icon
          message="功能权限和默认数据范围将原样复制，创建后可独立编辑。"
        />

        <template v-else>
          <a-form-item label="功能权限">
            <a-checkbox-group v-model:value="roleForm.permission_keys" class="permission-editor">
              <section v-for="group in allPermissionGroups" :key="group.label">
                <h4>{{ group.label }}</h4>
                <div class="permission-editor-grid">
                  <a-checkbox
                    v-for="permission in group.permissions"
                    :key="permission.key"
                    :value="permission.key"
                  >
                    <span>{{ permission.name }}</span>
                    <small>{{ permission.description }}</small>
                  </a-checkbox>
                </div>
              </section>
            </a-checkbox-group>
          </a-form-item>

          <a-form-item label="默认数据范围" required>
            <a-select
              v-model:value="roleForm.default_scope_type"
              :options="scopeOptions"
              @change="handleScopeChange"
            />
          </a-form-item>

          <a-form-item
            v-if="roleForm.default_scope_type === 'selected_organizations_and_descendants'"
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
        </template>
      </a-form>
    </a-modal>
  </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { useUserStore } from '@/stores/user'
import { getDepartments } from '@/apis/department_api'
import { copyRole, createRole, deactivateRole, getRoleOverview, updateRole } from '@/apis/role_api'
import {
  buildDepartmentTree,
  getDepartmentSelectionSummary,
  normalizeDepartmentSelection
} from '@/utils/departmentTree'
import {
  getDataScopeLabel,
  getRoleAuditActionLabel,
  groupRolePermissions
} from '@/utils/roleOverview'

const overview = ref({ permissions: [], data_scope_types: [], scope_departments: [], roles: [] })
const userStore = useUserStore()
const departments = ref([])
const selectedRoleId = ref(null)
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

const selectedRole = computed(
  () => overview.value.roles.find((role) => role.id === selectedRoleId.value) || null
)
const permissionGroups = computed(() =>
  groupRolePermissions(overview.value.permissions, selectedRole.value?.permission_keys || [])
)
const allPermissionGroups = computed(() =>
  groupRolePermissions(
    overview.value.permissions,
    overview.value.permissions.map((permission) => permission.key)
  )
)
const scopeOptions = computed(() =>
  overview.value.data_scope_types.map((scope) => ({ value: scope.key, label: scope.label }))
)
const departmentTree = computed(() => buildDepartmentTree(departments.value))
const scopeDepartments = computed(() =>
  departments.value.length ? departments.value : overview.value.scope_departments
)
const selectedScopeSummary = computed(() => {
  if (selectedRole.value?.default_scope_type === 'selected_organizations_and_descendants') {
    return getDepartmentSelectionSummary(
      scopeDepartments.value,
      selectedRole.value.default_department_ids
    )
  }
  return getDataScopeLabel(overview.value.data_scope_types, selectedRole.value?.default_scope_type)
})
const editorTitle = computed(
  () => ({ create: '创建角色', copy: '复制角色', edit: '编辑角色' })[editorMode.value]
)
const canManageRoles = computed(
  () => userStore.isSuperAdmin && userStore.hasPermission('role:manage')
)

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

const formatAuditTime = (value) => (value ? new Date(value).toLocaleString('zh-CN') : '')
const formatAuditValue = (value) => (value == null ? '无' : JSON.stringify(value, null, 2))

onMounted(() => {
  loadOverview()
  if (canManageRoles.value) loadDepartments()
})
</script>

<style scoped lang="less">
.role-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;

  h2,
  p {
    margin: 0;
  }

  h2 {
    color: var(--gray-900);
    font-size: 18px;
  }

  p {
    margin-top: 4px;
    color: var(--gray-500);
  }
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

.role-workspace {
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  min-height: 560px;
  overflow: hidden;
  border: 1px solid var(--gray-200);
  border-radius: 10px;
  background: var(--gray-0);
}

.role-list {
  padding: 8px;
  border-right: 1px solid var(--gray-200);
  background: var(--gray-50);
}

.role-list-item {
  display: flex;
  width: 100%;
  flex-direction: column;
  gap: 5px;
  padding: 11px 12px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--gray-800);
  text-align: left;
  cursor: pointer;

  &:hover {
    background: var(--gray-100);
  }

  &.active {
    background: var(--main-50);
    color: var(--main-700);
  }

  &:focus-visible {
    outline: 2px solid var(--main-400);
    outline-offset: 2px;
  }
}

.role-list-name {
  font-size: 14px;
  font-weight: 600;
}

.role-list-meta {
  display: flex;
  gap: 8px;
  color: var(--gray-500);
  font-size: 12px;
}

.role-detail {
  min-width: 0;
  padding: 20px;
  overflow: auto;
}

.role-detail-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;

  h2,
  p {
    margin: 0;
  }

  p {
    margin: 6px 0;
    color: var(--gray-500);
  }

  code {
    padding: 4px 7px;
    border-radius: 6px;
    background: var(--gray-100);
    color: var(--gray-600);
  }
}

.role-title-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.role-summary-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin: 20px 0;
}

.role-summary-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 14px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-50);

  span {
    color: var(--gray-500);
    font-size: 12px;
  }

  strong {
    color: var(--gray-800);
    font-size: 15px;
  }
}

.role-detail-section {
  padding-top: 18px;
  border-top: 1px solid var(--gray-150);

  & + & {
    margin-top: 22px;
  }

  h3 {
    margin: 0 0 12px;
    color: var(--gray-800);
    font-size: 15px;
  }
}

.permission-groups,
.role-audits {
  display: grid;
  gap: 16px;
}

.permission-group-title {
  margin-bottom: 7px;
  color: var(--gray-600);
  font-size: 13px;
  font-weight: 600;
}

.permission-list,
.role-members {
  display: grid;
  gap: 6px;
}

.permission-item {
  display: grid;
  grid-template-columns: 130px minmax(180px, 1fr) auto;
  align-items: center;
  gap: 12px;
  padding: 9px 10px;
  border-radius: 7px;
  background: var(--gray-50);

  code,
  .permission-description {
    color: var(--gray-500);
    font-size: 12px;
  }
}

.permission-name {
  color: var(--gray-800);
  font-size: 13px;
  font-weight: 500;
}

.role-member {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 10px;
  border-radius: 7px;
  background: var(--gray-50);

  code {
    color: var(--gray-500);
  }
}

.role-audit {
  padding: 12px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-50);

  p {
    margin: 4px 0 8px;
    color: var(--gray-500);
  }

  summary {
    color: var(--main-600);
    cursor: pointer;
  }
}

.role-audit-header {
  display: flex;
  justify-content: space-between;
  gap: 12px;

  span {
    color: var(--gray-500);
    font-size: 12px;
  }
}

.audit-values {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-top: 10px;

  span {
    color: var(--gray-500);
    font-size: 12px;
  }

  pre {
    max-height: 240px;
    margin: 4px 0 0;
    padding: 8px;
    overflow: auto;
    border-radius: 6px;
    background: var(--gray-0);
    color: var(--gray-700);
    font-size: 11px;
    white-space: pre-wrap;
  }
}

.role-form-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.permission-editor {
  display: grid;
  width: 100%;
  gap: 14px;

  h4 {
    margin: 0 0 6px;
    color: var(--gray-700);
    font-size: 13px;
  }
}

.permission-editor-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;

  :deep(.ant-checkbox-wrapper) {
    align-items: flex-start;
    margin-inline-start: 0;
    padding: 8px;
    border-radius: 6px;
    background: var(--gray-50);
  }

  span,
  small {
    display: block;
  }

  small {
    margin-top: 2px;
    color: var(--gray-500);
    font-size: 11px;
  }
}

@media (max-width: 900px) {
  .role-workspace {
    grid-template-columns: 1fr;
  }

  .role-list {
    display: flex;
    overflow-x: auto;
    border-right: 0;
    border-bottom: 1px solid var(--gray-200);
  }

  .role-list-item {
    min-width: 150px;
  }

  .permission-item,
  .role-form-row,
  .permission-editor-grid,
  .audit-values,
  .role-summary-grid {
    grid-template-columns: 1fr;
  }

  .role-detail-header,
  .role-toolbar {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
