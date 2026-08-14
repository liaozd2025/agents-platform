<template>
  <section class="role-management">
    <a-alert
      message="当前为只读总览"
      description="内置角色由系统维护。自定义角色和角色分配将在后续功能中开放。"
      type="info"
      show-icon
      class="role-readonly-alert"
    />

    <a-spin :spinning="loading">
      <div v-if="errorMessage" class="role-load-error" role="alert">
        <span>{{ errorMessage }}</span>
        <a-button size="small" @click="loadOverview">重新加载</a-button>
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
              <span>{{ role.is_builtin ? '内置' : '自定义' }}</span>
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
              <p>{{ selectedRole.description }}</p>
            </div>
            <code>{{ selectedRole.code }}</code>
          </header>

          <div class="role-summary-grid">
            <div class="role-summary-card">
              <span>默认数据范围</span>
              <strong>{{ selectedScopeLabel }}</strong>
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
            <div class="permission-groups">
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
        </article>
      </div>

      <a-empty v-else-if="!loading" description="暂无角色" />
    </a-spin>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { getRoleOverview } from '@/apis/role_api'
import { getDataScopeLabel, groupRolePermissions } from '@/utils/roleOverview'

const overview = ref({ permissions: [], data_scope_types: [], roles: [] })
const selectedRoleId = ref(null)
const loading = ref(false)
const errorMessage = ref('')

const selectedRole = computed(
  () => overview.value.roles.find((role) => role.id === selectedRoleId.value) || null
)
const selectedScopeLabel = computed(() =>
  getDataScopeLabel(overview.value.data_scope_types, selectedRole.value?.default_scope_type)
)
const permissionGroups = computed(() =>
  groupRolePermissions(overview.value.permissions, selectedRole.value?.permission_keys || [])
)

const loadOverview = async () => {
  loading.value = true
  errorMessage.value = ''
  try {
    overview.value = await getRoleOverview()
    selectedRoleId.value = overview.value.roles[0]?.id ?? null
  } catch (error) {
    errorMessage.value = error.message || '角色与权限加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(loadOverview)
</script>

<style scoped lang="less">
.role-management {
  .role-readonly-alert {
    margin-bottom: 16px;
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
    margin-top: 6px;
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

.permission-groups {
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

  .permission-item {
    grid-template-columns: 1fr;
    gap: 4px;
  }

  .role-summary-grid {
    grid-template-columns: 1fr;
  }
}
</style>
