import { createRouter, createWebHistory } from 'vue-router'
import { useUserStore } from '@/modules/identity/model/user.js'
import { canAccessRoute, getAuthenticatedHomePath } from '@/shared/lib/authNavigation.js'
import { resolveAppNavigationPath, resolveAppSurface } from '@/shared/composables/useEmbedMode.js'
import { sanitizeRedirect } from '@/modules/identity/model/oidcAutoStart.js'
import { SETTINGS_ROUTES } from '@/shared/lib/settingsNavigation.js'

/** 生成独立站与 OA 嵌入共用的设置路由。 */
const createSettingsRoutes = (embedded = false) =>
  SETTINGS_ROUTES.map(({ id, path, routeName, requiredPermission, requiredAnyPermissions }) => ({
    path: embedded ? path.slice(1) : path,
    name: embedded ? `Embed${routeName}` : routeName,
    component: () => import('@/modules/settings/ui/SettingsModal.vue'),
    meta: {
      keepAlive: false,
      requiresAuth: true,
      settingsTab: id,
      requiredPermission,
      requiredAnyPermissions
    }
  }))

const AppLayout = () => import('@/app/layouts/AppLayout.vue')

/** 将会话身份绑定到路由实例，保活页面不读取其他会话的路由参数。 */
const sessionRouteProps = (route) => ({
  memberSessionId: typeof route.query.member_session_id === 'string' ? route.query.member_session_id : '',
  threadId: typeof route.params.thread_id === 'string' ? route.params.thread_id : '',
  agentId: typeof route.query.agent_id === 'string' ? route.query.agent_id : '',
  projectId: typeof route.query.project_id === 'string' ? route.query.project_id : ''
})

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      redirect: '/agent'
    },
    {
      path: '/login',
      name: 'login',
      component: () => import('../../pages/LoginView.vue'),
      meta: { requiresAuth: false }
    },
    {
      path: '/auth/oidc/callback', // oidc登录回调页面
      name: 'OIDCCallback',
      component: () => import('@/pages/OIDCCallbackView.vue'),
      meta: { requiresAuth: false }
    },
    {
      path: '/auth/cli/authorize',
      name: 'CLIAuthAuthorize',
      component: () => import('@/pages/CLIAuthAuthorizeView.vue'),
      meta: { requiresAuth: true }
    },
    {
      path: '/auth/mcp/authorize',
      name: 'MCPAuthAuthorize',
      component: () => import('@/pages/MCPAuthAuthorizeView.vue'),
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
          component: () => import('@/pages/AgentView.vue'),
          props: sessionRouteProps,
          meta: { keepAlive: true, requiresAuth: true, requiredPermission: 'agent:use' }
        },
        {
          path: ':thread_id',
          name: 'AgentCompWithThreadId',
          component: () => import('@/pages/AgentView.vue'),
          props: sessionRouteProps,
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
          component: () => import('@/pages/AgentView.vue'),
          props: sessionRouteProps,
          meta: { keepAlive: true, requiresAuth: true, requiredPermission: 'agent:use' }
        },
        {
          path: ':thread_id',
          name: 'EmbedAgentWithThreadId',
          component: () => import('@/pages/AgentView.vue'),
          props: sessionRouteProps,
          meta: { keepAlive: true, requiresAuth: true, requiredPermission: 'agent:use' }
        },
        {
          path: 'agent-manage',
          name: 'EmbedAgentManageComp',
          component: () => import('@/pages/AgentManageView.vue'),
          meta: {
            keepAlive: false,
            requiresAuth: true,
            requiredAnyPermissions: ['agent:use', 'agent:manage', 'model_provider:manage', 'api_key:manage_all']
          }
        },
        {
          path: 'workspace',
          name: 'EmbedWorkspaceComp',
          component: () => import('@/pages/WorkspaceView.vue'),
          meta: { keepAlive: true, requiresAuth: true }
        },
        {
          path: 'dashboard',
          name: 'EmbedDashboardComp',
          component: () => import('@/pages/DashboardView.vue'),
          meta: { keepAlive: false, requiresAuth: true, requiredPermission: 'dashboard:view' }
        },
        {
          path: 'extensions',
          name: 'EmbedExtensionsComp',
          component: () => import('@/pages/ExtensionsView.vue'),
          meta: { keepAlive: false, requiresAuth: true },
          children: [
            {
              path: 'knowledge-bases/:kbId',
              alias: 'knowledgebase/:kbId',
              name: 'EmbedExtensionKnowledgeBaseDetail',
              component: () => import('@/pages/KnowledgeBaseInfoView.vue'),
              meta: {
                keepAlive: false,
                requiresAuth: true,
                requiredAnyPermissions: ['knowledge_base:read', 'knowledge_base:manage']
              }
            },
            {
              path: 'knowledge-bases/:kbId/evaluation/:datasetId',
              alias: 'knowledgebase/:kbId/evaluation/:datasetId',
              name: 'EmbedExtensionEvaluationBenchmarkDetail',
              component: () => import('@/pages/EvaluationBenchmarkDetailView.vue'),
              meta: {
                keepAlive: false,
                requiresAuth: true,
                requiredPermission: 'knowledge_evaluation:manage',
                requiredAnyPermissions: ['knowledge_base:read', 'knowledge_base:manage']
              }
            },
            {
              path: 'mcp/:slug',
              name: 'EmbedExtensionMcpDetail',
              component: () => import('@/modules/extensions/ui/McpDetailView.vue'),
              meta: {
                keepAlive: false,
                requiresAuth: true,
                requiredPermission: 'mcp:manage'
              }
            },
            {
              path: 'skill/:slug',
              name: 'EmbedExtensionSkillDetail',
              component: () => import('@/modules/extensions/ui/SkillDetailView.vue'),
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
          component: () => import('../../pages/WorkspaceView.vue'),
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
          component: () => import('@/pages/DashboardView.vue'),
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
          component: () => import('@/pages/AgentManageView.vue'),
          meta: {
            keepAlive: false,
            requiresAuth: true,
            requiredAnyPermissions: ['agent:use', 'agent:manage', 'model_provider:manage', 'api_key:manage_all']
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
          component: () => import('../../pages/ExtensionsView.vue'),
          meta: {
            keepAlive: false,
            requiresAuth: true
          },
          children: [
            {
              path: 'knowledge-bases/:kbId',
              name: 'ExtensionKnowledgeBaseDetail',
              component: () => import('../../pages/KnowledgeBaseInfoView.vue'),
              meta: {
                keepAlive: false,
                requiresAuth: true,
                requiredAnyPermissions: ['knowledge_base:read', 'knowledge_base:manage']
              }
            },
            {
              path: 'knowledge-bases/:kbId/evaluation/:datasetId',
              name: 'ExtensionEvaluationBenchmarkDetail',
              component: () => import('../../pages/EvaluationBenchmarkDetailView.vue'),
              meta: {
                keepAlive: false,
                requiresAuth: true,
                requiredPermission: 'knowledge_evaluation:manage',
                requiredAnyPermissions: ['knowledge_base:read', 'knowledge_base:manage']
              }
            },
            {
              path: 'mcp/:slug',
              name: 'ExtensionMcpDetail',
              component: () => import('../../modules/extensions/ui/McpDetailView.vue'),
              meta: {
                keepAlive: false,
                requiresAuth: true,
                requiredPermission: 'mcp:manage'
              }
            },
            {
              path: 'skill/:slug',
              name: 'ExtensionSkillDetail',
              component: () => import('../../modules/extensions/ui/SkillDetailView.vue'),
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
      component: () => import('../../pages/EmptyView.vue'),
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
