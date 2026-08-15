import { createRouter, createWebHistory } from 'vue-router'
import AppLayout from '@/layouts/AppLayout.vue'
import BlankLayout from '@/layouts/BlankLayout.vue'
import { useUserStore } from '@/stores/user'
import { canAccessRoute, getAuthenticatedHomePath } from '@/utils/authNavigation'
import { resolveAppNavigationPath, resolveAppSurface } from '@/composables/useEmbedMode'
import { sanitizeRedirect } from '@/utils/oidcAutoStart'
import { SETTINGS_ROUTES } from '@/utils/settingsNavigation'

/** 生成独立站与 OA 嵌入共用的设置路由。 */
const createSettingsRoutes = (embedded = false) =>
  SETTINGS_ROUTES.map(({ id, path, routeName, requiredPermission, requiredAnyPermissions }) => ({
    path: embedded ? path.slice(1) : path,
    name: embedded ? `Embed${routeName}` : routeName,
    component: () => import('@/components/SettingsModal.vue'),
    meta: {
      keepAlive: false,
      requiresAuth: true,
      settingsTab: id,
      requiredPermission,
      requiredAnyPermissions
    }
  }))

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'main',
      component: BlankLayout,
      children: [
        {
          path: '',
          name: 'Home',
          component: () => import('../views/HomeView.vue'),
          meta: { keepAlive: true, requiresAuth: false }
        }
      ]
    },
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/LoginView.vue'),
      meta: { requiresAuth: false }
    },
    {
      path: '/auth/oidc/callback', // oidc登录回调页面
      name: 'OIDCCallback',
      component: () => import('@/views/OIDCCallbackView.vue'),
      meta: { public: true }
    },
    {
      path: '/auth/cli/authorize',
      name: 'CLIAuthAuthorize',
      component: () => import('@/views/CLIAuthAuthorizeView.vue'),
      meta: { requiresAuth: true }
    },
    {
      path: '/agent',
      name: 'AgentMain',
      component: AppLayout,
      children: [
        {
          path: '',
          name: 'AgentComp',
          component: () => import('../views/AgentView.vue'),
          meta: { keepAlive: true, requiresAuth: true, requiredPermission: 'agent:use' }
        },
        {
          path: ':thread_id',
          name: 'AgentCompWithThreadId',
          component: () => import('../views/AgentView.vue'),
          meta: { keepAlive: true, requiresAuth: true, requiredPermission: 'agent:use' }
        }
      ]
    },
    {
      path: '/embed',
      name: 'EmbedMain',
      component: AppLayout,
      meta: { embed: true },
      children: [
        {
          path: '',
          name: 'EmbedAgent',
          component: () => import('../views/AgentView.vue'),
          meta: { keepAlive: true, requiresAuth: true, requiredPermission: 'agent:use' }
        },
        {
          path: ':thread_id',
          name: 'EmbedAgentWithThreadId',
          component: () => import('../views/AgentView.vue'),
          meta: { keepAlive: true, requiresAuth: true, requiredPermission: 'agent:use' }
        },
        {
          path: 'agent-manage',
          name: 'EmbedAgentManageComp',
          component: () => import('../views/AgentManageView.vue'),
          meta: {
            keepAlive: false,
            requiresAuth: true,
            requiredAnyPermissions: ['agent:use', 'agent:manage', 'model_provider:manage']
          }
        },
        {
          path: 'workspace',
          name: 'EmbedWorkspaceComp',
          component: () => import('../views/WorkspaceView.vue'),
          meta: { keepAlive: true, requiresAuth: true }
        },
        {
          path: 'dashboard',
          name: 'EmbedDashboardComp',
          component: () => import('../views/DashboardView.vue'),
          meta: { keepAlive: false, requiresAuth: true, requiredPermission: 'dashboard:view' }
        },
        {
          path: 'extensions',
          name: 'EmbedExtensionsComp',
          component: () => import('../views/ExtensionsView.vue'),
          meta: { keepAlive: false, requiresAuth: true },
          children: [
            {
              path: 'knowledgebase/:kbId',
              name: 'EmbedExtensionKnowledgeBaseDetail',
              component: () => import('../views/DataBaseInfoView.vue'),
              meta: {
                keepAlive: false,
                requiresAuth: true,
                requiredAnyPermissions: ['knowledge_base:read', 'knowledge_base:manage']
              }
            },
            {
              path: 'mcp/:slug',
              name: 'EmbedExtensionMcpDetail',
              component: () => import('../components/extensions/McpDetailView.vue'),
              meta: {
                keepAlive: false,
                requiresAuth: true,
                requiredPermission: 'mcp:manage'
              }
            },
            {
              path: 'skill/:slug',
              name: 'EmbedExtensionSkillDetail',
              component: () => import('../components/extensions/SkillDetailView.vue'),
              meta: {
                keepAlive: false,
                requiresAuth: true,
                requiredAnyPermissions: ['skill:use', 'skill:manage']
              }
            }
          ]
        },
        ...createSettingsRoutes(true)
      ]
    },
    {
      path: '/workspace',
      name: 'workspace',
      component: AppLayout,
      children: [
        {
          path: '',
          name: 'WorkspaceComp',
          component: () => import('../views/WorkspaceView.vue'),
          meta: { keepAlive: true, requiresAuth: true }
        }
      ]
    },
    {
      path: '/dashboard',
      name: 'dashboard',
      component: AppLayout,
      children: [
        {
          path: '',
          name: 'DashboardComp',
          component: () => import('../views/DashboardView.vue'),
          meta: { keepAlive: false, requiresAuth: true, requiredPermission: 'dashboard:view' }
        }
      ]
    },
    {
      path: '/agent-manage',
      name: 'agent-manage',
      component: AppLayout,
      children: [
        {
          path: '',
          name: 'AgentManageComp',
          component: () => import('../views/AgentManageView.vue'),
          meta: {
            keepAlive: false,
            requiresAuth: true,
            requiredAnyPermissions: ['agent:use', 'agent:manage', 'model_provider:manage']
          }
        }
      ]
    },
    {
      path: '/extensions',
      name: 'extensions',
      component: AppLayout,
      children: [
        {
          path: '',
          name: 'ExtensionsComp',
          component: () => import('../views/ExtensionsView.vue'),
          meta: {
            keepAlive: false,
            requiresAuth: true
          },
          children: [
            {
              path: 'knowledgebase/:kbId',
              name: 'ExtensionKnowledgeBaseDetail',
              component: () => import('../views/DataBaseInfoView.vue'),
              meta: {
                keepAlive: false,
                requiresAuth: true,
                requiredAnyPermissions: ['knowledge_base:read', 'knowledge_base:manage']
              }
            },
            {
              path: 'mcp/:slug',
              name: 'ExtensionMcpDetail',
              component: () => import('../components/extensions/McpDetailView.vue'),
              meta: {
                keepAlive: false,
                requiresAuth: true,
                requiredPermission: 'mcp:manage'
              }
            },
            {
              path: 'skill/:slug',
              name: 'ExtensionSkillDetail',
              component: () => import('../components/extensions/SkillDetailView.vue'),
              meta: {
                keepAlive: false,
                requiresAuth: true,
                requiredAnyPermissions: ['skill:use', 'skill:manage']
              }
            }
          ]
        }
      ]
    },
    ...createSettingsRoutes(),
    {
      path: '/:pathMatch(.*)*',
      name: 'NotFound',
      component: () => import('../views/EmptyView.vue'),
      meta: { requiresAuth: false }
    }
  ]
})

// 全局前置守卫
router.beforeEach(async (to, from) => {
  const embeddedTargetPath = resolveAppNavigationPath(
    resolveAppSurface(from) === 'oa-embed',
    to.path
  )
  if (embeddedTargetPath !== to.path) {
    return { path: embeddedTargetPath, query: to.query, hash: to.hash }
  }

  // 检查路由是否需要认证
  const requiresAuth = to.matched.some((record) => record.meta.requiresAuth === true)
  const isEmbedRoute = to.matched.some((record) => record.meta.embed === true)

  const userStore = useUserStore()

  // 如果有 token 但用户信息未加载，先获取用户信息
  if (!isEmbedRoute && userStore.token && !userStore.userId) {
    try {
      await userStore.getCurrentUser()
    } catch (error) {
      // 如果获取用户信息失败（如 token 过期），清除 token
      console.error('获取用户信息失败:', error)
      userStore.logout()
    }
  }

  const isLoggedIn = userStore.isLoggedIn

  // 嵌入页由 postMessage 握手后再渲染业务组件，不能在 yuxi:ready 前请求受保护接口。
  if (isEmbedRoute && !userStore.userId) return true

  // 如果路由需要认证但用户未登录
  if (requiresAuth && !isLoggedIn) {
    // 保存尝试访问的路径，登录后跳转
    sessionStorage.setItem('redirect', to.fullPath)
    return '/login'
  }

  const authenticatedHomePath = resolveAppNavigationPath(
    isEmbedRoute,
    getAuthenticatedHomePath(userStore.hasPermission)
  )

  if (!canAccessRoute(to.matched, userStore.hasPermission)) {
    return authenticatedHomePath
  }

  // 如果用户已登录但访问登录页，按 redirect 参数跳转
  if (to.path === '/login' && isLoggedIn) {
    return sanitizeRedirect(to.query.redirect)
  }

  // 其他情况正常导航
  return true
})

export default router
