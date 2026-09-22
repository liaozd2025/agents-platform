// 必须放在所有业务/依赖 import 之前：老内核（内网终端有 Chrome 86）缺少 at / findLast 等 ES2022+ 方法，
// 依赖内部一旦调用就会抛 TypeError 导致白屏，这里先补上再加载其余模块。
import '@/utils/legacyApiPolyfills'

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import piniaPluginPersistedstate from 'pinia-plugin-persistedstate'

import App from './App.vue'
import router from './router'

import Antd from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'
import '@/assets/css/main.css'

const app = createApp(App)
const pinia = createPinia()
pinia.use(piniaPluginPersistedstate)

// 低版本浏览器的告知由 index.html 顶部的友好提示条负责，这里一律照常挂载 ——
// 不能因为浏览器版本旧就把用户挡在门外（内网终端存在 Chrome 86 且无法升级）。
app.use(pinia)
app.use(router)
app.use(Antd)

app.mount('#app')
