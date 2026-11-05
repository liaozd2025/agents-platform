<script setup>
import { computed, ref, watch } from 'vue'
import { onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'

import PageHeader from '@/shared/ui/PageHeader.vue'
import AgentManagePanel from '@/modules/agents/ui/AgentManagePanel.vue'
import ModelProviderManagePanel from '@/modules/settings/ui/ModelProviderManagePanel.vue'
import ApiIntegrationPanel from '@/modules/settings/ui/ApiIntegrationPanel.vue'
import ScheduledAgentsView from '@/modules/agents/ui/ScheduledAgents.vue'
import { useUserStore } from '@/modules/identity/model/user'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const activeTab = ref(null)
const agentPanelRef = ref(null)
const providerPanelRef = ref(null)
const apiIntegrationPanelRef = ref(null)
const schedulePanelRef = ref(null)

const modelManageTabs = computed(() => {
  const tabs = []
  if (['agent:use', 'agent:manage'].some((permission) => userStore.hasPermission(permission))) {
    tabs.push({ key: 'agents', label: '智能体' }, { key: 'schedules', label: '定时任务 (beta)' })
  }
  if (userStore.hasPermission('model_provider:manage')) {
    tabs.push({ key: 'providers', label: '模型供应商' })
  }
  if (userStore.hasPermission('api_key:manage_all')) tabs.push({ key: 'integration', label: 'API 集成' })
  return tabs
})

const activePanel = computed(() => {
  if (activeTab.value === 'schedules') return schedulePanelRef.value
  if (activeTab.value === 'integration') return apiIntegrationPanelRef.value
  if (activeTab.value === 'providers') return providerPanelRef.value
  return agentPanelRef.value
})

const activeLoading = computed(
  () => activePanel.value?.loading || activePanel.value?.saving || false
)
const activeStats = computed(() => activePanel.value?.stats || {})

const normalizeTab = (tab) => {
  if (modelManageTabs.value.some((item) => item.key === tab)) return tab
  return modelManageTabs.value[0]?.key || null
}

watch(
  () => [route.query.tab, userStore.effectivePermissions],
  ([tab]) => {
    const nextTab = normalizeTab(tab)
    if (activeTab.value !== nextTab) activeTab.value = nextTab
  },
  { immediate: true }
)

function canChangeTab(nextTab) {
  if (activeTab.value !== 'schedules' || nextTab === 'schedules') return true
  return schedulePanelRef.value?.beforeLeave?.() ?? true
}

async function requestTabChange(item) {
  const nextTab = normalizeTab(item.key)
  if (nextTab === activeTab.value) return
  await router.replace({ query: { ...route.query, tab: nextTab } })
}

onBeforeRouteUpdate((to) => canChangeTab(normalizeTab(to.query.tab)))
</script>

<template>
  <div class="agent-manage-view">
    <PageHeader
      :active-key="activeTab"
      title="智能体管理"
      :tabs="modelManageTabs"
      :loading="activeLoading"
      :show-border="true"
      aria-label="智能体管理视图切换"
      @change="requestTabChange"
    >
      <template #info>
        <div v-if="activeTab === 'agents'" class="summary-strip">
          <span>{{ activeStats.total || 0 }} 个智能体</span>
          <span>{{ activeStats.manageable || 0 }} 个可编辑</span>
          <span v-if="!activeStats.manageable">可新建私有智能体</span>
        </div>
        <div v-else-if="activeTab === 'providers'" class="summary-strip">
          <span>{{ activeStats.enabled || 0 }} 个启用</span>
          <span v-if="activeStats.warning > 0" class="warning-count">
            {{ activeStats.warning }} 个凭证缺失
          </span>
          <span>{{ activeStats.models || 0 }} 个模型</span>
        </div>
      </template>
    </PageHeader>

    <div class="agent-manage-content">
      <div v-if="activeTab === 'agents'" class="tab-panel">
        <AgentManagePanel ref="agentPanelRef" />
      </div>
      <div
        v-if="userStore.hasPermission('model_provider:manage') && activeTab === 'providers'"
        class="tab-panel"
      >
        <ModelProviderManagePanel ref="providerPanelRef" />
      </div>
      <div v-if="userStore.hasPermission('api_key:manage_all') && activeTab === 'integration'" class="tab-panel">
        <ApiIntegrationPanel ref="apiIntegrationPanelRef" />
      </div>
      <div v-if="activeTab === 'schedules'" class="tab-panel schedule-tab-panel">
        <ScheduledAgentsView ref="schedulePanelRef" />
      </div>
    </div>
  </div>
</template>

<style lang="less" scoped>
.agent-manage-view {
  display: flex;
  flex-direction: column;
  min-height: 100%;
  background: var(--bg-page);
  color: var(--gray-1000);
}

.agent-manage-content {
  flex: 1;
  min-height: 0;
  overflow: hidden;

  .tab-panel {
    height: 100%;
    min-height: 0;
    overflow-y: auto;
  }

  .schedule-tab-panel {
    overflow: hidden;
  }
}

.summary-strip {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;

  span {
    color: var(--gray-700);
    font-size: 12px;
    line-height: 18px;
    white-space: nowrap;
  }

  .warning-count {
    padding: 2px 8px;
    border-radius: 4px;
    background: var(--color-warning-50);
    color: var(--color-warning-700);
  }
}
</style>
