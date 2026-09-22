<script setup>
import zhCN from 'ant-design-vue/es/locale/zh_CN'
// 显式引入而非依赖「app.use(Antd) 全局注册」，避免注册名变化导致本次修复静默失效
import { StyleProvider, legacyLogicalPropertiesTransformer } from 'ant-design-vue'
import { useThemeStore } from '@/stores/theme'

const themeStore = useThemeStore()

// ant-design-vue 4 的组件样式由运行时 CSS-in-JS 注入，默认 hashPriority='low'，
// 选择器会被拼成 `.ant-input:where(.css-hash)`。`:where()` 需要 Chrome 88+，
// 低版本浏览器不认识该伪类 → 整个选择器非法 → 该块组件样式被整条丢弃（表现为输入框、按钮完全没样式）。
// 这里做一次能力探测：支持 `:where()` 就保持默认的 'low'（行为与改动前完全一致），
// 不支持则退化为 'high'，让哈希变回真实类名（`.css-hash.ant-input`），把样式救回来。
// 代价：high 下 antd 选择器特异性升高，个别「全局单类覆盖」可能打平失效；
// 但这只发生在本来就完全没样式的老浏览器上，属于净收益。
// 默认按现代浏览器处理：非浏览器环境（SSR / 单测）没有 CSS 全局对象，不应误降级
let supportsWhereSelector = true
if (typeof CSS !== 'undefined' && typeof CSS.supports === 'function') {
  try {
    supportsWhereSelector = CSS.supports('selector(:where(a))')
  } catch {
    // CSS.supports 不支持 selector 语法或探测本身报错：按不支持处理，宁可多救一次样式
    supportsWhereSelector = false
  }
}

const hashPriority = supportsWhereSelector ? 'low' : 'high'

// antd 4 的组件样式里大量使用 CSS 逻辑属性（inset-block / padding-inline 等，需要 Chrome 87+），
// 而终端存在 Chrome 86（内网无 VPN 装不了新版），故用官方 transformer 额外输出一份物理属性。
// 官方实现默认保留原逻辑属性，因此现代浏览器（支持逻辑属性）行为不变，只是多一份兜底声明。
const styleTransformers = [legacyLogicalPropertiesTransformer]
</script>
<template>
  <style-provider :hash-priority="hashPriority" :transformers="styleTransformers">
    <a-config-provider :theme="themeStore.currentTheme" :locale="zhCN">
      <router-view />
    </a-config-provider>
  </style-provider>
</template>
