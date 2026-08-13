<script setup>
import zhCN from 'ant-design-vue/es/locale/zh_CN'
import { useEmbedMode } from '@/composables/useEmbedMode'
import { useAgentStore } from '@/stores/agent'
import { useUserStore } from '@/stores/user'
import { useThemeStore } from '@/stores/theme'
import { onMounted } from 'vue'

const agentStore = useAgentStore()
const userStore = useUserStore()
const themeStore = useThemeStore()
const embedMode = useEmbedMode()

onMounted(async () => {
  if (!embedMode.value && userStore.isLoggedIn) {
    await agentStore.initialize()
  }
})
</script>
<template>
  <a-config-provider :theme="themeStore.currentTheme" :locale="zhCN">
    <router-view />
  </a-config-provider>
</template>
