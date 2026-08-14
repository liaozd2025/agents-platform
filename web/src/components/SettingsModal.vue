<template>
  <a-modal
    v-model:open="visible"
    :title="null"
    width="100%"
    :style="{ top: 0, maxWidth: 'none', paddingBottom: 0 }"
    :footer="null"
    :closable="false"
    :keyboard="true"
    transition-name=""
    mask-transition-name=""
    @cancel="handleClose"
    class="settings-modal"
    wrap-class-name="settings-modal-wrap"
    :destroyOnClose="true"
    :bodyStyle="{ padding: 0 }"
  >
    <div class="settings-container">
      <aside class="settings-sider" aria-label="系统设置导航">
        <button type="button" class="settings-back-btn" @click="handleClose">
          <ArrowLeft :size="16" />
          <span>返回应用</span>
        </button>

        <a-input
          v-model:value="settingsSearch"
          allow-clear
          placeholder="搜索设置..."
          class="settings-search"
          aria-label="搜索设置"
        >
          <template #prefix><Search :size="15" /></template>
        </a-input>

        <nav class="settings-sider-nav">
          <section v-for="group in navigationGroups" :key="group.label" class="nav-group">
            <div class="nav-group-label">{{ group.label }}</div>
            <button
              v-for="item in group.items"
              :key="item.id"
              type="button"
              class="sider-item"
              :class="{ activesec: activeTab === item.id }"
              :aria-current="activeTab === item.id ? 'page' : undefined"
              @click="activeTab = item.id"
            >
              <component :is="settingsTabIcons[item.id]" class="icon" :size="17" />
              <span>{{ item.label }}</span>
            </button>
          </section>

          <a-empty
            v-if="!navigationGroups.length"
            :image="null"
            description="未找到设置项"
            class="settings-nav-empty"
          />
        </nav>

        <div class="settings-sider-footer">
          <div v-if="showStarCard" class="settings-star-card">
            <div class="star-card-header">
              <div class="star-card-badge">
                <Star :size="12" />
                <span>支持项目</span>
              </div>
              <button
                type="button"
                class="star-card-close lucide-icon-btn"
                aria-label="关闭 Star 提示"
                @click="dismissStarCard"
              >
                <X :size="14" />
              </button>
            </div>
            <p class="star-card-title">给 Yuxi 点个 Star</p>
            <p class="star-card-description">
              如果这个项目帮到了你，欢迎去 GitHub 点亮一个 Star，让更多人看到它。
            </p>
            <a
              class="star-card-link"
              :href="projectRepoUrl"
              target="_blank"
              rel="noopener noreferrer"
            >
              <img
                class="star-card-link-image"
                src="https://img.shields.io/github/stars/xerrors/Yuxi?label=Yuxi&style=social"
                alt="GitHub stars for Yuxi"
              />
              <ExternalLink :size="13" />
            </a>
          </div>

          <div v-if="userStore.isLoggedIn" class="settings-user-summary">
            <FallbackAvatar
              :src="userStore.avatar"
              :name="userStore.username"
              :seed="userStore.uid || userStore.username"
              kind="user"
              :size="28"
              shape="circle"
              :alt="userStore.username"
            />
            <span class="summary-name">{{ userStore.username || userStore.uid }}</span>
            <span class="summary-role">{{ userRoleText }}</span>
          </div>
        </div>
      </aside>

      <header class="settings-mobile-header">
        <button type="button" class="settings-back-btn" @click="handleClose">
          <ArrowLeft :size="16" />
          <span>返回应用</span>
        </button>
        <span class="mobile-title">系统设置</span>
      </header>

      <nav class="settings-mobile-nav" aria-label="系统设置导航">
        <button
          v-for="item in mobileNavigationItems"
          :key="item.id"
          type="button"
          class="nav-item"
          :class="{ active: activeTab === item.id }"
          :aria-current="activeTab === item.id ? 'page' : undefined"
          @click="activeTab = item.id"
        >
          {{ item.label }}
        </button>
      </nav>

      <main class="settings-content-wrapper">
        <div class="settings-content">
          <div v-show="activeTab === 'account'" v-if="userStore.isLoggedIn">
            <AccountSettingsComponent />
          </div>

          <div v-if="activeTab === 'apiKeys' && userStore.isLoggedIn">
            <ApiKeyManagementComponent />
          </div>

          <div v-if="activeTab === 'agentEnv' && userStore.isLoggedIn">
            <AgentEnvSettingsCard />
          </div>

          <div v-show="activeTab === 'base'" v-if="userStore.isAdmin">
            <div class="settings-page-header">
              <div class="settings-page-title">基本设置</div>
              <p class="settings-page-description">配置系统默认模型、内容审查与服务链接。</p>
            </div>
            <BasicSettingsSection />
          </div>

          <div v-show="activeTab === 'ocr'" v-if="userStore.isAdmin">
            <div class="settings-page-header">
              <div class="settings-page-title">OCR 配置</div>
              <p class="settings-page-description">配置系统默认 OCR 方法及相关服务参数。</p>
            </div>
            <OCRSettingsSection />
          </div>

          <div v-if="activeTab === 'user' && userStore.hasPermission('user:read')">
            <UserManagementComponent />
          </div>

          <div v-show="activeTab === 'department'" v-if="userStore.isSuperAdmin">
            <DepartmentManagementComponent />
          </div>

          <div v-if="activeTab === 'role' && userStore.hasPermission('role:read')">
            <div class="settings-page-header">
              <div class="settings-page-title">角色与权限</div>
              <p class="settings-page-description">
                查看内置角色的功能权限、默认数据范围和当前成员。
              </p>
            </div>
            <RoleManagementComponent />
          </div>
        </div>
      </main>
    </div>
  </a-modal>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useUserStore } from '@/stores/user'
import {
  ArrowLeft,
  CircleUser,
  ExternalLink,
  Key,
  ScanText,
  Search,
  Settings,
  ShieldCheck,
  SquareTerminal,
  Star,
  User,
  Users,
  X
} from 'lucide-vue-next'
import AccountSettingsComponent from '@/components/AccountSettingsComponent.vue'
import AgentEnvSettingsCard from '@/components/AgentEnvSettingsCard.vue'
import BasicSettingsSection from '@/components/BasicSettingsSection.vue'
import OCRSettingsSection from '@/components/OCRSettingsSection.vue'
import ApiKeyManagementComponent from '@/components/ApiKeyManagementComponent.vue'
import UserManagementComponent from '@/components/UserManagementComponent.vue'
import DepartmentManagementComponent from '@/components/DepartmentManagementComponent.vue'
import RoleManagementComponent from '@/components/RoleManagementComponent.vue'
import FallbackAvatar from '@/components/common/FallbackAvatar.vue'
import { getSettingsNavigationGroups } from '@/utils/settingsNavigation'

const props = defineProps({
  visible: {
    type: Boolean,
    default: false
  },
  initialTab: {
    type: String,
    default: ''
  }
})

const emit = defineEmits(['update:visible', 'close'])

const settingsTabIcons = {
  account: CircleUser,
  agentEnv: SquareTerminal,
  base: Settings,
  user: User,
  department: Users,
  role: ShieldCheck,
  apiKeys: Key,
  ocr: ScanText
}
const roleLabels = {
  superadmin: '超级管理员',
  admin: '管理员',
  user: '普通用户'
}

const userStore = useUserStore()
const activeTab = ref('account')
const settingsSearch = ref('')
const showStarCard = ref(true)

const STAR_CARD_STORAGE_KEY = 'yuxi-settings-star-card-dismissed'
const projectRepoUrl = 'https://github.com/xerrors/Yuxi'

const visible = computed({
  get: () => props.visible,
  set: (value) => emit('update:visible', value)
})
const permissions = computed(() => ({
  isLoggedIn: userStore.isLoggedIn,
  isAdmin: userStore.isAdmin,
  isSuperAdmin: userStore.isSuperAdmin,
  effectivePermissions: userStore.effectivePermissions
}))
const allNavigationGroups = computed(() => getSettingsNavigationGroups(permissions.value))
const navigationGroups = computed(() =>
  getSettingsNavigationGroups(permissions.value, settingsSearch.value)
)
const mobileNavigationItems = computed(() =>
  allNavigationGroups.value.flatMap((group) => group.items)
)
const availableTabs = computed(() => mobileNavigationItems.value.map((item) => item.id))
const userRoleText = computed(() => roleLabels[userStore.userRole] || '普通用户')

const setActiveTab = (preferredTab) => {
  if (preferredTab && availableTabs.value.includes(preferredTab)) {
    activeTab.value = preferredTab
    return
  }
  activeTab.value = availableTabs.value.includes('base') ? 'base' : availableTabs.value[0]
}

const handleClose = () => {
  emit('close')
}

const dismissStarCard = () => {
  showStarCard.value = false
  localStorage.setItem(STAR_CARD_STORAGE_KEY, 'true')
}

onMounted(() => {
  showStarCard.value = localStorage.getItem(STAR_CARD_STORAGE_KEY) !== 'true'
})

watch(
  () => [props.visible, props.initialTab],
  ([isVisible]) => {
    if (isVisible) {
      settingsSearch.value = ''
      setActiveTab(props.initialTab)
    }
  }
)
</script>

<style lang="less">
.settings-modal-wrap {
  overflow: hidden;
}

.settings-modal.ant-modal {
  width: 100%;
  height: 100dvh;
  margin: 0;

  .ant-modal-content {
    display: flex;
    width: 100%;
    height: 100dvh;
    padding: 0;
    overflow: hidden;
    border-radius: 0;
    box-shadow: none;
  }

  .ant-modal-body {
    flex: 1;
    min-width: 0;
    min-height: 0;
  }
}

.settings-modal .settings-container {
  display: flex;
  width: 100%;
  height: 100dvh;
  overflow: hidden;
  background: var(--gray-0);
}

.settings-modal .settings-back-btn {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-height: 36px;
  padding: 8px 10px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--gray-700);
  font-size: 14px;
  cursor: pointer;

  &:hover {
    background: var(--gray-150);
    color: var(--gray-900);
  }

  &:focus-visible {
    outline: 2px solid var(--main-400);
    outline-offset: 2px;
  }
}

.settings-modal .settings-sider {
  display: flex;
  flex: 0 0 264px;
  flex-direction: column;
  height: 100%;
  padding: 14px 12px;
  overflow-y: auto;
  border-right: 1px solid var(--gray-150);
  background: var(--gray-50);

  .settings-back-btn {
    width: 100%;
  }
}

.settings-modal .settings-search {
  margin: 8px 2px 12px;

  &.ant-input-affix-wrapper {
    height: 34px;
    border-color: var(--gray-200);
    border-radius: 8px;
    background: var(--gray-0);

    .ant-input-prefix {
      color: var(--gray-500);
    }
  }
}

.settings-modal .settings-sider-nav {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.settings-modal .nav-group + .nav-group {
  margin-top: 8px;
}

.settings-modal .nav-group-label {
  padding: 8px 10px 4px;
  color: var(--gray-500);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.06em;
}

.settings-modal .sider-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 36px;
  padding: 8px 10px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--gray-700);
  font-size: 14px;
  text-align: left;
  cursor: pointer;

  &:hover {
    background: var(--gray-150);
  }

  &:focus-visible {
    outline: 2px solid var(--main-400);
    outline-offset: -2px;
  }

  &.activesec {
    background: var(--gray-200);
    color: var(--main-700);
    font-weight: 500;
  }

  .icon {
    flex-shrink: 0;
  }
}

.settings-modal .settings-nav-empty {
  margin-top: 28px;

  .ant-empty-description {
    color: var(--gray-500);
    font-size: 13px;
  }
}

.settings-modal .settings-sider-footer {
  margin-top: auto;
  padding-top: 12px;
}

.settings-modal .settings-star-card {
  padding: 12px;
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 10px;
  background: var(--gray-0);

  .star-card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  .star-card-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--main-700);
    font-size: 12px;
    font-weight: 600;
  }

  .star-card-close {
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    border: none;
    border-radius: 50%;
    background: transparent;
    color: var(--gray-600);
    cursor: pointer;

    &:hover {
      background: var(--gray-150);
      color: var(--gray-900);
    }

    &:focus-visible {
      outline: 2px solid var(--main-400);
      outline-offset: 1px;
    }
  }

  .star-card-title {
    margin: 9px 0 4px;
    color: var(--gray-900);
    font-size: 14px;
    font-weight: 600;
  }

  .star-card-description {
    margin: 0;
    color: var(--gray-600);
    font-size: 12px;
    line-height: 1.5;
  }

  .star-card-link {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    margin-top: 10px;
    color: var(--gray-600);
    text-decoration: none;
  }

  .star-card-link-image {
    display: block;
    height: 20px;
  }
}

.settings-modal .settings-user-summary {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-top: 12px;
  padding: 12px 10px 2px;
  border-top: 1px solid var(--gray-150);
  color: var(--gray-700);
  font-size: 13px;

  .summary-name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .summary-role {
    margin-left: auto;
    color: var(--gray-500);
    font-size: 12px;
    white-space: nowrap;
  }
}

.settings-modal .settings-content-wrapper {
  flex: 1;
  min-width: 0;
  height: 100%;
  overflow-y: auto;
  background: var(--gray-0);
}

.settings-modal .settings-content {
  width: 100%;
  max-width: 880px;
  min-height: 100%;
  margin: 0 auto;
  padding: 56px 48px 96px;

  .model-providers-section,
  .user-management,
  .department-management,
  .apikey-management {
    min-height: auto;
  }

  .settings-page-header {
    margin-bottom: 26px;
  }

  .header-section {
    display: flex;
    justify-content: space-between;
    gap: 16px;
  }

  .header-content {
    flex: 1;
    min-width: 0;
  }

  .settings-page-title {
    margin: 0;
    color: var(--gray-900);
    font-size: 26px;
    font-weight: 600;
    line-height: 1.3;
  }

  .settings-page-description {
    margin: 7px 0 0;
    color: var(--gray-600);
    font-size: 14px;
    line-height: 1.55;
  }

  .section-subtitle {
    margin: 0;
    color: var(--gray-900);
    font-size: 16px;
    font-weight: 500;
  }

  .add-btn {
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
    gap: 6px;
  }

  .account-settings,
  .agent-env-settings,
  .apikey-management,
  .user-management,
  .department-management {
    // 子模块使用 scoped 样式，这里统一全屏设置中心的页面标题层级。
    > .header-section {
      align-items: flex-start !important;
      margin-bottom: 26px !important;

      > .header-content {
        > .section-title {
          margin: 0 !important;
          color: var(--gray-900) !important;
          font-size: 26px !important;
          font-weight: 600 !important;
          line-height: 1.3 !important;
        }

        > .section-description {
          margin: 7px 0 0 !important;
          color: var(--gray-600) !important;
          font-size: 14px !important;
          line-height: 1.55 !important;
        }
      }
    }
  }
}

.settings-modal .settings-mobile-header,
.settings-modal .settings-mobile-nav {
  display: none;
}

@media (max-width: 900px) {
  .settings-modal .settings-container {
    flex-direction: column;
  }

  .settings-modal .settings-sider {
    display: none;
  }

  .settings-modal .settings-mobile-header {
    position: relative;
    display: flex;
    flex-shrink: 0;
    align-items: center;
    min-height: 52px;
    padding: 8px 12px;
    border-bottom: 1px solid var(--gray-150);
    background: var(--gray-50);

    .mobile-title {
      position: absolute;
      left: 50%;
      color: var(--gray-900);
      font-size: 15px;
      font-weight: 600;
      transform: translateX(-50%);
    }
  }

  .settings-modal .settings-mobile-nav {
    display: flex;
    flex-shrink: 0;
    padding: 0 8px;
    overflow-x: auto;
    border-bottom: 1px solid var(--gray-150);
    background: var(--gray-0);
    scrollbar-width: none;

    &::-webkit-scrollbar {
      display: none;
    }

    .nav-item {
      flex-shrink: 0;
      padding: 12px 14px 10px;
      border: none;
      border-bottom: 2px solid transparent;
      background: transparent;
      color: var(--gray-600);
      font-size: 14px;
      font-weight: 500;
      white-space: nowrap;
      cursor: pointer;

      &.active {
        border-bottom-color: var(--main-color);
        color: var(--main-color);
      }

      &:focus-visible {
        outline: 2px solid var(--main-400);
        outline-offset: -3px;
      }
    }
  }

  .settings-modal .settings-content-wrapper {
    height: auto;
  }

  .settings-modal .settings-content {
    max-width: none;
    padding: 28px 20px 64px;

    .settings-page-title {
      font-size: 22px;
    }

    .account-settings,
    .agent-env-settings,
    .apikey-management,
    .user-management,
    .department-management {
      > .header-section {
        flex-wrap: wrap;
        margin-bottom: 22px !important;

        > .header-content > .section-title {
          font-size: 22px !important;
        }
      }
    }
  }
}
</style>
