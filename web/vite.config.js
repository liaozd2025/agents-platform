import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import { parseOAEmbedAllowedOrigins } from './src/utils/oaEmbedBridge.js'
import { providers } from '@opencode-ai/models/snapshot'

// 快照只在构建进程读取，浏览器只接收展示字段；模型覆盖随锁定依赖更新。
const modelMetadataPlugin = {
  name: 'model-display-metadata',
  resolveId(id) {
    if (id === 'virtual:model-display-metadata') return '\0' + id
  },
  load(id) {
    if (id !== '\0virtual:model-display-metadata') return
    const catalog = Object.fromEntries(
      Object.entries(providers).map(([providerId, provider]) => [
        providerId,
        {
          models: Object.fromEntries(
            Object.entries(provider.models).map(([modelId, model]) => [
              modelId,
              {
                modalities: { input: model.modalities?.input },
                limit: { context: model.limit?.context },
                cost: model.cost
              }
            ])
          )
        }
      ])
    )
    return `export const providers = ${JSON.stringify(catalog)}`
  }
}

export default defineConfig(({ mode }) => {
  // eslint-disable-next-line no-undef
  const env = loadEnv(mode, process.cwd(), '')
  const rawEmbedOrigins = env.VITE_YUXI_EMBED_ALLOWED_ORIGINS || ''
  const configuredEmbedOrigins = rawEmbedOrigins.split(/[\s,]+/).filter(Boolean)
  const embedOrigins = parseOAEmbedAllowedOrigins(rawEmbedOrigins)
  if (configuredEmbedOrigins.some((origin) => !embedOrigins.includes(origin))) {
    throw new Error('VITE_YUXI_EMBED_ALLOWED_ORIGINS 只能包含精确的 HTTP origin')
  }
  return {
    plugins: [vue(), modelMetadataPlugin],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url))
      }
    },
    /*
     * 浏览器基线：终端存在 Chrome 86（内网没有 VPN 装不了新版浏览器），且要求「任何版本页面都要能渲染出来」，
     * 因此把构建目标从 Vite 8 默认的 chrome111 显式降到 chrome79（Vite/esbuild 可稳定降级的实用下限）。
     * - JS：rolldown/esbuild 按该目标降级语法（可选链、空值合并、逻辑赋值等都会被改写）；
     * - CSS：minify 阶段由 lightningcss 按同目标降级，可自动改写 inset / aspect-ratio / 原生嵌套 /
     *   逻辑属性等 —— 项目里 28 处 inset 因此不必手改。
     * 注意 two 点：
     * 1. antd 的组件样式是运行时注入的，不经过这里，由 App.vue 的 StyleProvider（hashPriority +
     *    逻辑属性 transformer）单独处理；
     * 2. 源码层面已经手工去掉了 85+ 语法（逻辑赋值）并补齐了 API polyfill，
     *    所以即使构建期降级未覆盖到的环境，页面也能渲染出来（可能样式降级，由顶部提示条告知用户）。
     */
    build: {
      target: ['chrome79'],
      cssTarget: ['chrome79']
    },
    /*
     * dev 模式专用：Vite 会先把依赖预构建到 node_modules/.vite/deps，其默认目标是 esnext 基线（不降级）。
     * 实测某个依赖产物里含逻辑赋值（||=，需 Chrome 85+），内网老内核会直接抛
     * SyntaxError: Unexpected token '='（报错定位在 .vite/deps/xxx.js），整个应用起不来。
     * 这里把预构建目标对齐到同一基线，保证「开发态也能在老内核上把页面渲染出来」。
     * 注意配置位：Vite 8 的依赖预构建已改用 rolldown，必须写 optimizeDeps.rolldownOptions.transform.target
     * —— optimizeDeps.esbuildOptions.target 在这个版本上不生效（踩过）。
     * 改这个值后需要让 dev server 重新预构建：重启 web 容器即可（缓存会自动失效重建）。
     */
    optimizeDeps: {
      rolldownOptions: {
        transform: {
          target: 'chrome79'
        }
      }
    },
    server: {
      headers: {
        'Content-Security-Policy': `frame-ancestors 'self'${embedOrigins.length ? ` ${embedOrigins.join(' ')}` : ''}`
      },
      proxy: {
        '^/.well-known/oauth-': {
          target: env.VITE_API_URL || 'http://api:5050',
          changeOrigin: true
        },
        '^/api/mcp(?:/|$)': {
          target: env.VITE_API_URL || 'http://api:5050',
          changeOrigin: false
        },
        '^/api': {
          target: env.VITE_API_URL || 'http://api:5050',
          changeOrigin: true
        },
        '^/minio/public/': {
          target: env.VITE_MINIO_URL || 'http://minio:9000',
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/minio/, '')
        }
      },
      watch: {
        usePolling: true,
        ignored: ['**/node_modules/**', '**/dist/**']
      },
      host: '0.0.0.0'
    }
  }
})
