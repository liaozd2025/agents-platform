<template>
  <div class="extensions-view extension-page-root">
    <PageHeader
      v-if="!isDetailPage"
      v-model:active-key="activeTab"
      title="智能体扩展"
      :tabs="extensionTabs"
      :loading="activeChildLoading"
      :show-border="true"
      aria-label="智能体扩展视图切换"
    />

    <div v-if="!isDetailPage" class="extensions-content">
      <div v-if="canAccessKnowledge && activeTab === 'knowledge'" class="tab-panel">
        <DataBaseView ref="knowledgeRef" embedded />
      </div>
      <div v-if="userStore.hasPermission('tool:manage') && activeTab === 'tools'" class="tab-panel">
        <ToolsCardList ref="toolsRef" />
      </div>
      <div v-if="canAccessSkills && activeTab === 'skills'" class="tab-panel">
        <SkillCardList ref="skillsRef" />
      </div>
      <div v-if="userStore.hasPermission('mcp:manage') && activeTab === 'mcp'" class="tab-panel">
        <McpCardList ref="mcpRef" />
      </div>
    </div>

    <router-view v-else />
  </div>
</template>

<script setup>
import { ref, watch, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ToolsCardList from '@/components/extensions/ToolsCardList.vue'
import McpCardList from '@/components/extensions/McpCardList.vue'
import SkillCardList from '@/components/extensions/SkillCardList.vue'
import PageHeader from '@/components/shared/PageHeader.vue'
import DataBaseView from '@/views/DataBaseView.vue'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const activeTab = ref(null)
const knowledgeRef = ref(null)
const skillsRef = ref(null)
const mcpRef = ref(null)
const toolsRef = ref(null)
const canAccessKnowledge = computed(() =>
  ['knowledge_base:read', 'knowledge_base:manage'].some((permission) =>
    userStore.hasPermission(permission)
  )
)
const canAccessSkills = computed(() =>
  ['skill:use', 'skill:manage'].some((permission) => userStore.hasPermission(permission))
)

const extensionTabs = computed(() => {
  const tabs = []
  if (canAccessKnowledge.value) {
    tabs.push({ key: 'knowledge', label: '知识库' })
  }
  if (canAccessSkills.value) tabs.push({ key: 'skills', label: '技能' })
  if (userStore.hasPermission('tool:manage')) tabs.push({ key: 'tools', label: '工具' })
  if (userStore.hasPermission('mcp:manage')) tabs.push({ key: 'mcp', label: 'MCP' })
  return tabs
})
const allowedTabKeys = computed(() => extensionTabs.value.map((tab) => tab.key))
const defaultTabKey = computed(() => extensionTabs.value[0]?.key || '')

const normalizeTab = (tab) => {
  if (allowedTabKeys.value.includes(tab)) return tab
  return defaultTabKey.value
}

const replaceTabQuery = (tab) => {
  const query = { ...route.query }
  if (tab === defaultTabKey.value) {
    delete query.tab
  } else {
    query.tab = tab
  }
  router.replace({ query })
}

const isDetailPage = computed(() => {
  return (
    route.path.startsWith('/extensions/knowledgebase/') ||
    route.path.startsWith('/extensions/mcp/') ||
    route.path.startsWith('/extensions/skill/')
  )
})

const activeChildLoading = computed(() => {
  const refMap = {
    knowledge: knowledgeRef,
    tools: toolsRef,
    skills: skillsRef,
    mcp: mcpRef
  }
  const child = refMap[activeTab.value]
  return child?.value?.loading || false
})

watch(
  () => [route.query.tab, userStore.effectivePermissions],
  ([tab]) => {
    const nextTab = normalizeTab(tab)
    if (activeTab.value !== nextTab) activeTab.value = nextTab
    if (tab && tab !== nextTab) replaceTabQuery(nextTab)
  },
  { immediate: true }
)

watch(activeTab, (tab) => {
  if (!tab) return
  const nextTab = normalizeTab(tab)
  if (nextTab !== tab) {
    activeTab.value = nextTab
    return
  }
  if (route.query.tab === nextTab || (!route.query.tab && nextTab === defaultTabKey.value)) return
  replaceTabQuery(nextTab)
})
</script>

<style scoped lang="less">
@import '@/assets/css/extensions.less';

.extensions-view {
  .extensions-content {
    flex: 1;
    min-height: 0;
    overflow: hidden;

    .tab-panel {
      height: 100%;
      min-height: 0;
      overflow-y: auto;
    }
  }
}
</style>
