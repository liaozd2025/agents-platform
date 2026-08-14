import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import { parseOAEmbedAllowedOrigins } from './src/utils/oaEmbedBridge.js'

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
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url))
      }
    },
    server: {
      headers: {
        'Content-Security-Policy': `frame-ancestors 'self'${embedOrigins.length ? ` ${embedOrigins.join(' ')}` : ''}`
      },
      proxy: {
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
