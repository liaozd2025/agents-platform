<script setup>
import { ref, onMounted, onUnmounted, computed, provide, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import {
  BarChart3,
  ClipboardList,
  LibraryBig,
  Box,
  HardDrive,
  PanelLeft,
  PanelLeftOpen,
  PanelRight,
  MessageCirclePlus,
  Maximize2,
  PictureInPicture2,
  Search,
  X
} from '@lucide/vue'

import { useConfigStore } from '@/modules/settings/model/config.js'
import { useProjectsStore } from '@/modules/projects'
const projectsStore = useProjectsStore()
import { useAgentStore } from '@/modules/agents/model/agent.js'
import { useChatThreadsStore } from '@/modules/session/model/chatThreads.js'
import { useChatUIStore } from '@/modules/session/model/chatUI.js'
import { useKnowledgeBaseStore } from '@/modules/knowledge/model/knowledgeBase.js'
import { useInfoStore } from '@/modules/settings/model/info.js'
import { useBackgroundJobsStore } from '@/modules/background-jobs/model/jobs.js'
import { useUserStore } from '@/modules/identity/model/user.js'
import {
  resolveAppNavigationPath,
  shouldShowAppSidebar,
  useEmbedContext
} from '@/shared/composables/useEmbedMode.js'
import { useOAEmbedBridge } from '@/app/composables/useOAEmbedBridge.js'
import { storeToRefs } from 'pinia'
import SessionNavigation from '@/modules/session/ui/SessionNavigation.vue'
import { createSessionRouteCache } from './sessionRouteCache'
import UserInfoComponent from '@/modules/settings/ui/UserInfoComponent.vue'
import TaskCenterDrawer from '@/modules/background-jobs/ui/BackgroundJobsDrawer.vue'
import { SETTINGS_ROUTES } from '@/shared/lib/settingsNavigation.js'

const configStore = useConfigStore()
const agentStore = useAgentStore()
const chatThreadsStore = useChatThreadsStore()
const chatUIStore = useChatUIStore()
const knowledgeBaseStore = useKnowledgeBaseStore()
const infoStore = useInfoStore()
const jobsStore = useBackgroundJobsStore()
const userStore = useUserStore()
const route = useRoute()
const router = useRouter()
const {
  isEmbedded,
  displayMode: embedDisplayMode,
  modeConfirmed: embedModeConfirmed
} = useEmbedContext()
const oaEmbedBridge = useOAEmbedBridge(isEmbedded)
const {
  isAuthorized: embedAuthorized,
  statusMessage: embedStatusMessage,
  requestDisplayMode,
  requestClose
} = oaEmbedBridge
provide('oaEmbedBridge', oaEmbedBridge)
const { activeCount: activeCountRef, isDrawerOpen } = storeToRefs(jobsStore)
const { currentThreadId } = storeToRefs(chatThreadsStore)
const navigationRef = ref(null)
const sessionRouteCache = createSessionRouteCache(router)
const conversationRouteNames = new Set([
  'AgentComp',
  'AgentCompWithThreadId',
  'EmbedAgent',
  'EmbedAgentWithThreadId'
])
const embedModeOptions = [
  { value: 'fixed', label: '固定模式', icon: PanelRight },
  { value: 'floating', label: '浮窗模式', icon: PictureInPicture2 },
  { value: 'fullscreen', label: '全屏模式', icon: Maximize2 }
]

const { sidebarCollapsed } = storeToRefs(chatUIStore)
const embedSidebarCollapsed = ref(false)
const embedHistoryOpen = ref(false)
watch(() => route.fullPath, () => { embedHistoryOpen.value = false })
const isSettingsRoute = computed(() => Boolean(route.meta.settingsTab))
// 设置页已经提供专属导航，隐藏应用主侧栏，避免全屏嵌入时出现双侧栏。
const showSidebar = computed(
  () => !isSettingsRoute.value && shouldShowAppSidebar(isEmbedded.value, embedDisplayMode.value)
)
const layoutSidebarCollapsed = computed(() =>
  isEmbedded.value ? embedSidebarCollapsed.value : sidebarCollapsed.value
)
const canUseAgents = computed(() => userStore.hasPermission('agent:use'))
const canAccessExtensions = computed(() =>
  [
    'knowledge_base:read',
    'knowledge_base:manage',
    'skill:use',
    'skill:manage',
    'tool:manage',
    'mcp:manage'
  ].some((permission) => userStore.hasPermission(permission))
)

// Provide settings modal methods to child components
const openSettingsModal = (tab) => {
  const tabId = tab || (userStore.hasPermission('system_config:manage') ? 'base' : 'account')
  const target = SETTINGS_ROUTES.find((item) => item.id === tabId)
  if (!target) return

  router.push({ path: target.path, query: { returnTo: route.fullPath } })
}

const getRemoteConfig = async () => {
  try {
    await configStore.refreshConfig()
  } catch (error) {
    console.warn('加载系统配置失败:', error)
  }
}

const getRemoteDatabase = async () => {
  try {
    await knowledgeBaseStore.loadKnowledgeBases()
  } catch (error) {
    console.warn('加载知识库列表失败:', error)
  }
}

let layoutInitialization = null
let layoutInitializationUserId = null

const initializeLayout = () => {
  if (layoutInitialization) return layoutInitialization

  layoutInitialization = (async () => {
    // 加载信息配置与知识库数据无依赖，可并行
    const databaseRequest = userStore.hasPermission('knowledge_base:read')
      ? getRemoteDatabase()
      : null
    void infoStore.loadInfoConfig()
    void databaseRequest
    if (canUseAgents.value) await initAgentNavigation()
    if (userStore.hasPermission('system_config:manage')) await getRemoteConfig()
    // 仅管理员加载任务中心数据
    if (userStore.hasPermission('system_task:manage')) {
      jobsStore.loadJobs()
    }
  })()

  return layoutInitialization
}

const initializeLayoutWhenReady = () => {
  const currentUserId = userStore.userId || null
  if (layoutInitializationUserId !== currentUserId) {
    layoutInitialization = null
    layoutInitializationUserId = currentUserId
  }
  if (!isEmbedded.value || (embedAuthorized.value && userStore.userId)) {
    void initializeLayout()
  }
}

const handleGlobalKeydown = (event) => {
  if (
    (event.ctrlKey || event.metaKey) &&
    event.shiftKey &&
    event.key.toLowerCase() === 'd' &&
    userStore.hasPermission('system_log:read')
  ) {
    event.preventDefault()
    infoStore.showDebugModal = !infoStore.showDebugModal
  }
}

onMounted(() => {
  window.addEventListener('keydown', handleGlobalKeydown)
  initializeLayoutWhenReady()
  startThreadStatusSync()
})

// 低频刷新侧边栏线程状态，让后台线程完成时也能从 loading 转为 ready/done。
const THREAD_STATUS_SYNC_INTERVAL_MS = 12 * 1000
let threadStatusSyncTimer = null

const startThreadStatusSync = () => {
  if (threadStatusSyncTimer) return
  threadStatusSyncTimer = setInterval(() => {
    if (
      !canUseAgents.value || (isEmbedded.value && !embedAuthorized.value) ||
      layoutSidebarCollapsed.value ||
      (typeof document !== 'undefined' && document.visibilityState !== 'visible')
    ) {
      return
    }
    void chatThreadsStore.syncThreadStatuses()
  }, THREAD_STATUS_SYNC_INTERVAL_MS)
}

onUnmounted(() => {
  sessionRouteCache.dispose()
  // AppLayout 卸载会销毁 OA bridge；记录路由和登录态，区分正常切页与异常重建。
  console.warn('[OA iframe][诊断] AppLayout 卸载', {
    path: route.fullPath,
    userId: userStore.userId,
    isEmbedded: isEmbedded.value
  })
  window.removeEventListener('keydown', handleGlobalKeydown)
  if (threadStatusSyncTimer) {
    clearInterval(threadStatusSyncTimer)
    threadStatusSyncTimer = null
  }
})
watch([embedAuthorized, () => userStore.userId], initializeLayoutWhenReady)

const activeTaskCount = computed(() => activeCountRef.value || 0)
const isConversationRoute = computed(() => conversationRouteNames.has(route.name))

// 侧边栏品牌位的可用宽度只有约 100px（230px 侧边栏要减去头像与右侧两个操作按钮），
// 因此优先取配置里的短名 branding.sidebar_name，避免「九典制药智能体平台」这类全称被省略号截断。
const sidebarBrandName = computed(() => {
  // 品牌配置未就绪时返回空串（配合模板 v-if 整体不渲染），
  // 避免先闪一帧兜底文字再跳成正式配置内容
  if (!infoStore.isLoaded) return ''
  return (
    infoStore.branding.sidebar_name ||
    infoStore.organization.name ||
    infoStore.branding.name ||
    '智能体平台'
  )
})

// 下面是导航菜单部分，添加智能体项
const mainList = computed(() => {
  const items = []

  if (canUseAgents.value) {
    items.push({
      name: '新建对话',
      path: resolveAppNavigationPath(isEmbedded.value, '/agent'),
      icon: MessageCirclePlus,
      activeIcon: MessageCirclePlus,
      action: true,
      exactActive: true
    })
  }

  if (
    ['agent:use', 'agent:manage', 'model_provider:manage', 'api_key:manage_all'].some((permission) =>
      userStore.hasPermission(permission)
    )
  ) {
    items.push({
      name: '智能体',
      path: resolveAppNavigationPath(isEmbedded.value, '/agent-manage'),
      icon: Box,
      activeIcon: Box
    })
  }

  items.push({
    name: '个人空间',
    path: resolveAppNavigationPath(isEmbedded.value, '/workspace'),
    icon: HardDrive,
    activeIcon: HardDrive
  })

  if (canAccessExtensions.value) {
    items.push({
      name: '知识库 · 技能',
      path: resolveAppNavigationPath(isEmbedded.value, '/extensions'),
      activePaths: [resolveAppNavigationPath(isEmbedded.value, '/extensions')],
      icon: LibraryBig,
      activeIcon: LibraryBig
    })
  }

  if (userStore.hasPermission('dashboard:view')) {
    items.push({
      name: '数据总览',
      path: resolveAppNavigationPath(isEmbedded.value, '/dashboard'),
      icon: BarChart3,
      activeIcon: BarChart3
    })
  }

  return items
})

const primaryNavItem = computed(() => mainList.value[0] || null)
const secondaryNavItems = computed(() => mainList.value.slice(1))

const isNavItemActive = (item) => {
  const activePaths = item.activePaths || [item.path]
  if (item.exactActive) {
    return activePaths.some((path) => route.path === path)
  }
  return activePaths.some((path) => route.path === path || route.path.startsWith(`${path}/`))
}

const setSidebarCollapsed = (collapsed) => {
  if (isEmbedded.value) {
    embedSidebarCollapsed.value = collapsed
    return
  }
  sidebarCollapsed.value = collapsed
}

const toggleSidebar = () => {
  setSidebarCollapsed(!layoutSidebarCollapsed.value)
}

const openConversationSearch = () => {
  navigationRef.value?.openSessionSearch()
}

const initAgentNavigation = async () => {
  try {
    if (!agentStore.isInitialized) {
      await agentStore.initialize()
    }
    await Promise.all([chatThreadsStore.loadThreads(), projectsStore.loadProjects()])
  } catch (error) {
    console.warn('加载对话导航失败:', error)
  }
}

/** 根据当前运行形态返回对话路由名。 */
const getAgentRouteName = (withThread = false) => {
  if (isEmbedded.value) return withThread ? 'EmbedAgentWithThreadId' : 'EmbedAgent'
  return withThread ? 'AgentCompWithThreadId' : 'AgentComp'
}

/** 在非对话页切回窄档前先恢复当前对话。 */
const requestEmbedMode = async (mode) => {
  if (mode !== 'fullscreen' && !isConversationRoute.value) {
    await router.push({
      name: getAgentRouteName(Boolean(currentThreadId.value)),
      params: currentThreadId.value ? { thread_id: currentThreadId.value } : undefined
    })
  }
  requestDisplayMode(mode, currentThreadId.value)
}

const requestEmbedClose = () => requestClose(currentThreadId.value)

// Provide settings modal methods to child components
provide('settingsModal', {
  openSettingsModal
})
</script>

<template>
  <div
    class="app-layout"
    :class="{
      'sidebar-collapsed': layoutSidebarCollapsed,
      'embed-layout': isEmbedded
    }"
  >
    <div v-if="isEmbedded && embedAuthorized && !showSidebar" class="embed-session-actions">
      <button type="button" aria-label="新建会话" @click="router.push({ name: 'EmbedAgent' })"><MessageCirclePlus :size="18" /></button>
      <button type="button" aria-label="会话历史" @click="embedHistoryOpen = true"><PanelLeft :size="18" /></button>
    </div>
    <a-drawer v-if="isEmbedded && embedAuthorized" v-model:open="embedHistoryOpen" title="会话历史" placement="left" :width="300" :body-style="{ padding: '0', display: 'flex', minHeight: '0', overflow: 'hidden' }">
      <SessionNavigation class="embed-session-history" :visible="embedHistoryOpen" />
    </a-drawer>
    <div v-if="showSidebar" class="header">
      <div class="sidebar-brand" @click.stop>
        <div v-if="!layoutSidebarCollapsed" class="brand-identity">
          <!-- avatar 为空字符串时 <img src=""> 会指向页面自身并渲染成裂图（品牌配置接口未返回时的瞬间），
               必须用 v-if 挡住；文字侧由 sidebarBrandName 的「智能体平台」兜底 -->
          <img v-if="infoStore.organization.avatar" :src="infoStore.organization.avatar" class="brand-avatar" />
          <span v-if="sidebarBrandName" class="brand-name">{{ sidebarBrandName }}</span>
        </div>
        <button
          v-else
          type="button"
          class="brand-identity brand-expand-button"
          aria-label="展开侧边栏"
          @click="setSidebarCollapsed(false)"
        >
          <img v-if="infoStore.organization.avatar" :src="infoStore.organization.avatar" class="brand-avatar brand-avatar-image" />
          <PanelLeftOpen class="brand-expand-icon" size="20" />
        </button>
        <div
          v-if="!layoutSidebarCollapsed"
          class="sidebar-header-actions"
          aria-label="侧边栏操作"
        >
          <button
            type="button"
            class="sidebar-header-action"
            :class="{ active: navigationRef?.sessionSearchOpen }"
            aria-label="搜索"
            @click="openConversationSearch"
          >
            <Search size="17" />
          </button>
          <button
            type="button"
            class="sidebar-header-action"
            aria-label="折叠侧边栏"
            @click="toggleSidebar"
          >
            <PanelLeft size="17" />
          </button>
        </div>
      </div>
      <div class="nav">
        <RouterLink
          v-if="primaryNavItem"
          :to="primaryNavItem.path"
          class="nav-item"
          :class="{ active: isNavItemActive(primaryNavItem) }"
          :active-class="primaryNavItem.action ? '' : 'active'"
          @click.stop
        >
          <a-tooltip placement="right" :open="layoutSidebarCollapsed ? undefined : false">
            <template #title>{{ primaryNavItem.name }}</template>
            <component
              class="icon"
              :is="
                isNavItemActive(primaryNavItem) ? primaryNavItem.activeIcon : primaryNavItem.icon
              "
              size="18"
            />
          </a-tooltip>
          <span class="nav-text">{{ primaryNavItem.name }}</span>
        </RouterLink>

        <button
          v-if="layoutSidebarCollapsed"
          type="button"
          class="nav-item"
          :class="{ active: navigationRef?.sessionSearchOpen }"
          aria-label="搜索"
          @click.stop="openConversationSearch"
        >
          <a-tooltip placement="right" title="搜索">
            <Search class="icon" size="18" />
          </a-tooltip>
        </button>

        <RouterLink
          v-for="(item, index) in secondaryNavItems"
          :key="index"
          :to="item.path"
          v-show="!item.hidden"
          class="nav-item"
          :class="{ active: isNavItemActive(item) }"
          :active-class="item.action ? '' : 'active'"
          @click.stop
        >
          <a-tooltip placement="right" :open="layoutSidebarCollapsed ? undefined : false">
            <template #title>{{ item.name }}</template>
            <component
              class="icon"
              :is="isNavItemActive(item) ? item.activeIcon : item.icon"
              size="18"
            />
          </a-tooltip>
          <span class="nav-text">{{ item.name }}</span>
        </RouterLink>
      </div>
      <div class="fill">
        <SessionNavigation ref="navigationRef" v-if="canUseAgents && (!isEmbedded || embedAuthorized)" :visible="!layoutSidebarCollapsed" />
      </div>
      <div class="foo">
        <!-- 用户信息组件 -->
        <div class="nav-item user-info" @click.stop>
          <UserInfoComponent :show-role="!layoutSidebarCollapsed" :allow-logout="!isEmbedded">
            <template v-if="userStore.hasPermission('system_task:manage')" #actions>
              <a-tooltip placement="top" title="任务中心">
                <button
                  class="user-task-center"
                  :class="{ active: isDrawerOpen }"
                  type="button"
                  aria-label="任务中心"
                  @click.stop="jobsStore.openDrawer()"
                >
                  <a-badge
                    :count="activeTaskCount"
                    :overflow-count="99"
                    class="task-center-badge"
                    size="small"
                  >
                    <ClipboardList class="icon" size="16" />
                  </a-badge>
                </button>
              </a-tooltip>
            </template>
          </UserInfoComponent>
        </div>
      </div>
    </div>
    <div
      v-if="isEmbedded && !embedAuthorized"
      id="app-router-view"
      class="embed-auth-waiting"
      role="status"
    >
      <span class="embed-auth-spinner" aria-hidden="true"></span>
      <span>{{ embedStatusMessage }}</span>
    </div>
    <main v-else id="app-router-view">
      <router-view v-slot="{ Component, route }">
        <keep-alive>
          <component
            :is="Component"
            v-if="route.meta.keepAlive !== false"
            :key="sessionRouteCache.key(route)"
            @session-created="sessionRouteCache.adopt"
          />
        </keep-alive>
        <component :is="Component" v-if="route.meta.keepAlive === false" />
      </router-view>
    </main>
    <div
      v-if="isEmbedded && embedAuthorized"
      class="embed-page-controls"
      role="group"
      aria-label="AI 助手显示模式"
      :aria-busy="!embedModeConfirmed"
    >
      <button
        v-for="option in embedModeOptions"
        :key="option.value"
        type="button"
        class="embed-page-control"
        :class="{ active: embedDisplayMode === option.value }"
        :title="option.label"
        :aria-label="`切换为${option.label}`"
        :aria-pressed="embedDisplayMode === option.value"
        @click="requestEmbedMode(option.value)"
      >
        <component :is="option.icon" :size="16" aria-hidden="true" />
      </button>
      <button
        type="button"
        class="embed-page-control embed-page-close"
        title="关闭 AI 助手"
        aria-label="关闭 AI 助手"
        @click="requestEmbedClose"
      >
        <X :size="16" aria-hidden="true" />
      </button>
    </div>

    <TaskCenterDrawer v-if="userStore.hasPermission('system_task:manage')" />
  </div>
</template>

<style lang="less" scoped>
// Less 变量定义
@sidebar-width: 230px;
@sidebar-collapsed-width: 52px;
@sidebar-padding-y: 6px;
@sidebar-padding-x: 8px;
@sidebar-border-width: 1px;
@sidebar-item-height: 32px;
@sidebar-item-padding-x: 6px;
@sidebar-icon-size: 16px;
@brand-avatar-size: 24px;
@sidebar-collapsed-content-width: @sidebar-collapsed-width - @sidebar-border-width;
@sidebar-collapsed-icon-padding-x: (
  (@sidebar-collapsed-content-width - @sidebar-icon-size - (2 * @sidebar-border-width)) / 2
);
@sidebar-collapsed-avatar-padding-x: (
  (@sidebar-collapsed-content-width - @sidebar-item-height - (2 * @sidebar-border-width)) / 2
);
@sidebar-collapsed-brand-padding-x: ((@sidebar-collapsed-content-width - @brand-avatar-size) / 2);
@sidebar-collapsed-brand-icon-padding-x: (
  (@sidebar-collapsed-content-width - @sidebar-icon-size) / 2
);

.app-layout {
  display: flex;
  flex-direction: row;
  width: 100%;
  height: 100vh;
  min-width: var(--min-width);
}

.app-layout.embed-layout {
  min-width: 0;
}

div.header,
#app-router-view {
  height: 100%;
  max-width: 100%;
}

#app-router-view {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
}

main#app-router-view {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  grid-template-rows: minmax(0, 1fr);
}

.embed-auth-waiting {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 24px;
  color: var(--gray-600);
  background: var(--gray-25);
  font-size: 14px;
  text-align: center;
}

// OA 授权完成前使用轻量旋转指示，避免等待区域看起来像页面卡住。
.embed-auth-spinner {
  width: 24px;
  height: 24px;
  border: 2px solid var(--gray-200);
  border-top-color: var(--main-color);
  border-radius: 50%;
  animation: embed-auth-spin 0.8s linear infinite;
}

@keyframes embed-auth-spin {
  to {
    transform: rotate(360deg);
  }
}

.embed-page-controls {
  position: fixed;
  top: 10px;
  right: 14px;
  z-index: 20;
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 3px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-0);
  box-shadow: 0 2px 8px rgb(0 0 0 / 6%);
}

.embed-page-control {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 0;
  border-radius: 6px;
  color: var(--gray-600);
  background: transparent;
  cursor: pointer;
}

.embed-page-control:hover,
.embed-page-control:focus-visible,
.embed-page-control.active {
  color: var(--main-600);
  background: var(--main-50);
  outline: none;
}

.embed-page-close {
  margin-left: 2px;
}

.header {
  display: flex;
  flex-direction: column;
  flex: 0 0 @sidebar-width;
  justify-content: flex-start;
  align-items: stretch;
  gap: 0;
  background-color: var(--main-5);
  height: 100%;
  width: @sidebar-width;
  border-right: 1px solid var(--gray-100);
  overflow: hidden;
  user-select: none;
  transition:
    width 0.18s ease,
    flex-basis 0.18s ease;

  .nav {
    display: flex;
    flex: 0 0 auto;
    flex-direction: column;
    justify-content: flex-start;
    align-items: stretch;
    position: relative;
    gap: 2px;
    margin-top: 12px;
    padding: 0 @sidebar-padding-x;
  }

  .sidebar-conversations {
    height: 100%;
    min-height: 0;
    overflow: hidden;
    padding-left: @sidebar-padding-x;
  }

  .sidebar-brand,
  :deep(.conversation-nav-section:not(.sidebar-conversations)),
  .user-info {
    flex-shrink: 0;
  }

  .fill {
    flex: 1 1 0;
    min-height: 0;
  }

  .foo {
    position: relative;
    z-index: 1;
    flex: 0 0 auto;
    background: var(--main-5);
    padding: 0 @sidebar-padding-x @sidebar-padding-y;
  }

  .sidebar-brand {
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: @sidebar-item-height;
    margin-top: @sidebar-padding-y;
    padding: 0 @sidebar-padding-x;
    gap: 8px;
  }

  .brand-identity {
    display: flex;
    flex: 1 1 auto;
    align-items: center;
    min-width: 0;
    height: @sidebar-item-height;
    color: var(--gray-900);
    text-decoration: none;
    border: 0;
    background: transparent;
    padding: 0 4px;
  }

  button.brand-identity {
    cursor: pointer;
  }

  .brand-avatar {
    flex: 0 0 @brand-avatar-size;
    width: @brand-avatar-size;
    height: @brand-avatar-size;
    border-radius: 6px;
    object-fit: cover;
  }

  /*
   * 侧边栏品牌位可用宽度被固定尺寸挤出来：
   * @sidebar-width 230px - 2px 边框 - 16px 侧边栏内边距 - 8px 本行内边距
   * - 28px 头像 - 本行 margin-left - 8px 行内 gap - 62px 右侧两个 30px 操作按钮 ≈ 98px。
   * 当前文案「智能体平台」（配置 branding.sidebar_name，5 个汉字）14px 下实测约占 79px，
   * 可完整显示；文案一旦加长会先撑满再被 text-overflow 截成省略号，改文案前先核对这条宽度预算。
   */
  .brand-name {
    min-width: 0;
    margin-left: 10px;
    overflow: hidden;
    color: var(--gray-1000);
    font-size: 14px;
    font-weight: 650;
    line-height: 20px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .sidebar-header-actions {
    display: inline-flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 2px;
  }

  .sidebar-header-action {
    display: inline-flex;
    flex: 0 0 30px;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    border: 0;
    border-radius: 7px;
    background: transparent;
    color: var(--gray-600);
    cursor: pointer;
    transition:
      background-color 0.2s ease,
      border-color 0.2s ease,
      color 0.2s ease;

    &:hover,
    &:focus-visible {
      background: var(--main-20);
      color: var(--main-color);
      outline: none;
    }
    &.active {
      background: var(--main-20);
      color: var(--main-color);
    }
  }

  .nav-item {
    display: flex;
    align-items: center;
    justify-content: flex-start;
    width: 100%;
    height: @sidebar-item-height;
    padding: 0 @sidebar-item-padding-x;
    border: 1px solid transparent;
    border-radius: 8px;
    background-color: transparent;
    color: var(--gray-700);
    font-size: 14px;
    font-weight: 450;
    transition:
      background-color 0.2s ease-in-out,
      border-color 0.2s ease-in-out,
      color 0.2s ease-in-out;
    margin: 0;
    text-decoration: none;
    cursor: pointer;
    outline: none;

    .icon {
      flex: 0 0 @sidebar-icon-size;
      width: @sidebar-icon-size;
      height: @sidebar-icon-size;
    }

    .nav-text {
      min-width: 0;
      max-width: 140px;
      margin-left: 8px;
      overflow: hidden;
      line-height: 20px;
      font-weight: 450;
      text-overflow: ellipsis;
      white-space: nowrap;
      transition:
        opacity 0.12s ease,
        margin-left 0.18s ease,
        max-width 0.18s ease;
    }

    & > svg:focus {
      outline: none;
    }
    & > svg:focus-visible {
      outline: none;
    }

    &.primary-action {
      margin-bottom: 8px;
      border-color: var(--gray-150);
      background-color: var(--gray-0);
      color: var(--main-color);
      box-shadow: 0 3px 4px rgba(0, 10, 20, 0.02);

      &:hover {
        border-color: var(--gray-200);
        background-color: var(--gray-0);
        color: var(--gray-900);
        box-shadow: 0 3px 4px rgba(0, 10, 20, 0.07);
      }
    }

    &.warning {
      color: var(--color-error-500);
    }

    &:hover {
      border-color: transparent;
      background-color: var(--gray-50);
      color: var(--gray-900);
    }

    &.active {
      border-color: transparent;
      /* 原写法 color-mix(in srgb, var(--gray-100) 6%, var(--gray-100))：同色混合恒等于本身，
         直接写 var(--gray-100)，顺带避开 color-mix 在 Chrome 86 上不可用的问题 */
      background-color: var(--gray-100);
      font-weight: 600;
      color: var(--gray-1000);
    }

    &.api-docs {
      padding: 10px 12px;
    }
    &.docs {
      display: none;
    }
    &.theme-toggle-nav {
      .theme-toggle-icon {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 100%;
        height: 100%;
        cursor: pointer;
        color: var(--gray-1000);
        transition: color 0.2s ease-in-out;

        &:hover {
          color: var(--main-color);
        }
      }
    }
    &.user-info {
      margin-bottom: 8px;
      padding: 0 3px;
      overflow: hidden;

      :deep(.user-info-component) {
        width: 100%;
      }

      :deep(.user-info-dropdown) {
        width: 100%;
        height: @sidebar-item-height;
        border-radius: 8px;
        transition:
          background-color 0.2s ease,
          color 0.2s ease;
      }

      :deep(.user-info-dropdown:hover) {
        background: var(--main-20);
        color: var(--main-color);
      }
      :deep(.user-name) {
        flex: 1 1 auto;
      }

      :deep(.user-task-center) {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 28px;
        height: 28px;
        padding: 0;
        border: 1px solid transparent;
        border-radius: 6px;
        background: transparent;
        color: var(--gray-600);
        cursor: pointer;
        transition:
          background-color 0.2s ease,
          color 0.2s ease;

        &:hover,
        &.active {
          background: var(--main-30);
          color: var(--main-color);
        }

        .task-center-badge {
          display: flex;
          justify-content: center;
        }

        .icon {
          display: block;
          width: 16px;
          height: 16px;
        }
      }
    }
  }
}

.app-layout.sidebar-collapsed {
  .header {
    flex-basis: @sidebar-collapsed-width;
    width: @sidebar-collapsed-width;
    align-items: stretch;

    .sidebar-brand {
      justify-content: flex-start;
      width: 100%;
      margin-top: @sidebar-padding-y;
      padding: 0;
    }

    .brand-expand-button {
      flex: 0 0 100%;
      justify-content: flex-start;
      width: 100%;
      padding: 0;
      border-radius: 8px;

      .brand-avatar-image {
        margin-left: @sidebar-collapsed-brand-padding-x;
      }

      .brand-expand-icon {
        display: none;
        margin-left: @sidebar-collapsed-brand-icon-padding-x;
        width: @sidebar-icon-size;
        height: @sidebar-icon-size;
        color: var(--main-color);
      }

      &:hover,
      &:focus-visible {
        background: var(--main-20);
        outline: none;

        .brand-avatar-image {
          display: none;
        }

        .brand-expand-icon {
          display: block;
        }
      }
    }

    .nav {
      align-items: stretch;
      width: 100%;
      padding: 0;
    }

    .foo {
      padding: 0 0 @sidebar-padding-y;
    }

    .nav-item {
      justify-content: flex-start;
      width: 100%;
      padding: 0 @sidebar-collapsed-icon-padding-x;

      .nav-text {
        max-width: 0;
        margin-left: 0;
        opacity: 0;
        pointer-events: none;
      }

      &.user-info {
        padding: 0 @sidebar-collapsed-avatar-padding-x;

        :deep(.user-info-component),
        :deep(.user-info-dropdown) {
          justify-content: flex-start;
        }

        :deep(.user-info-actions) {
          display: none;
        }
      }
    }
  }
}
.embed-session-actions { position: fixed; top: 10px; left: 10px; z-index: 30; display: flex; gap: 4px; }
.embed-session-actions button { display: grid; place-items: center; width: 30px; height: 30px; border: 1px solid var(--gray-150); border-radius: 6px; color: var(--gray-600); background: var(--gray-0); cursor: pointer; }
.embed-session-history { display: flex; flex-direction: column; flex: 1; min-height: 0; overflow: hidden; }
</style>
