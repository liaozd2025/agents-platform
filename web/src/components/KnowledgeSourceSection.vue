<template>
  <div class="source-section">
    <div class="section-title">知识库来源 ({{ articleCount }})</div>
    <KbResultGroupedList :chunks="chunks" :show-summary="false" />
  </div>
</template>

<script setup>
import KbResultGroupedList from '@/components/sources/KbResultGroupedList.vue'
import { computed } from 'vue'
import { groupKnowledgeChunks } from '@/utils/kbResultGroups.js'

const props = defineProps({
  chunks: {
    type: Array,
    default: () => []
  }
})

const articleCount = computed(() => groupKnowledgeChunks(props.chunks).length)
</script>

<style scoped lang="less">
.source-section {
  .section-title {
    font-size: 12px;
    color: var(--gray-700);
    margin-bottom: 8px;
    font-weight: 600;
  }
}
</style>
