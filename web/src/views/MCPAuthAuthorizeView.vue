<template>
  <main class="mcp-consent">
    <section>
      <h1>授权知识库访问</h1>
      <a-alert v-if="errorMessage" type="error" :message="errorMessage" show-icon />
      <a-spin v-else-if="loading" />
      <template v-else>
        <p>允许 {{ session.client_name }} 以你的身份读取知识库。</p>
        <p>只能读取你有权限的资料，不允许上传、修改或删除。授权 30 天后到期。</p>
        <p>确认这是你发起的连接。授权完成后返回：</p>
        <code>{{ session.redirect_uri }}</code>
        <div class="actions">
          <a-button @click="router.replace('/agent')">取消</a-button>
          <a-button type="primary" :loading="approving" @click="approve">确认授权</a-button>
        </div>
      </template>
    </section>
  </main>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { authApi } from '@/apis/auth_api'

const route = useRoute()
const router = useRouter()
const session = ref(null)
const loading = ref(true)
const approving = ref(false)
const errorMessage = ref('')
const requestId = String(route.query.request || '')

onMounted(async () => {
  try {
    if (!requestId) throw new Error('缺少授权请求，请从智能体重新连接')
    session.value = await authApi.getMCPConsent(requestId)
  } catch (error) {
    errorMessage.value = error.message || '授权请求加载失败'
  } finally {
    loading.value = false
  }
})

async function approve() {
  approving.value = true
  try {
    const result = await authApi.approveMCPConsent(requestId)
    window.location.assign(result.redirect_uri)
  } catch (error) {
    errorMessage.value = error.message || '授权失败，请重新连接'
  } finally {
    approving.value = false
  }
}
</script>

<style scoped lang="less">
.mcp-consent {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
  background: var(--gray-0);
  color: var(--color-text);
  section {
    width: min(520px, 100%);
    padding: 24px;
    border: 1px solid var(--gray-150);
    border-radius: 8px;
  }
  code {
    overflow-wrap: anywhere;
  }
  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 12px;
    margin-top: 24px;
  }
}
</style>
