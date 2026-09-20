<template>
  <a-modal
    :open="open"
    :title="citation?.title || '引用来源'"
    :footer="null"
    width="720px"
    :body-style="{ maxHeight: '65vh', overflowY: 'auto' }"
    @cancel="emit('update:open', false)"
  >
    <div class="answer-citation-detail">
      <section
        v-for="(excerpt, index) in citation?.excerpts || []"
        :key="index"
        class="citation-excerpt"
        :aria-label="`原文片段 ${index + 1}`"
      >
        <p class="excerpt-label">
          原文片段 {{ index + 1 }}
          <span v-if="excerpt.start_line">
            · 第 {{ excerpt.start_line
            }}<template v-if="excerpt.end_line && excerpt.end_line !== excerpt.start_line"
              >–{{ excerpt.end_line }}</template
            >
            行</span
          >
        </p>
        <blockquote>{{ excerpt.text }}</blockquote>
      </section>
      <p v-if="!citation?.excerpts?.length" class="citation-missing" role="status">
        未获取到原文片段
      </p>
      <div class="citation-actions">
        <a-button v-if="citation?.kb_id && citation?.file_id" @click="fileDetailOpen = true">
          查看完整文档
        </a-button>
        <a
          v-if="citation?.url"
          :href="citation.url"
          target="_blank"
          rel="noopener noreferrer"
          @click="openSource"
          >打开来源网页</a
        >
        <span
          v-if="!citation?.url && !(citation?.kb_id && citation?.file_id)"
          class="citation-missing"
        >
          无可用来源入口
        </span>
      </div>
    </div>
  </a-modal>
  <FileDetailModal
    v-if="fileDetailOpen"
    v-model:open="fileDetailOpen"
    :kb-id="citation?.kb_id || ''"
    :file-id="citation?.file_id || ''"
  />
</template>

<script setup>
import { defineAsyncComponent, ref, watch } from 'vue'
import { navigateSource } from '@/utils/sourceNavigation.js'

const FileDetailModal = defineAsyncComponent(() => import('@/components/FileDetailModal.vue'))
const props = defineProps({
  open: { type: Boolean, default: false },
  citation: { type: Object, default: null }
})
const emit = defineEmits(['update:open'])
const fileDetailOpen = ref(false)
watch(
  () => props.citation?.source,
  () => {
    fileDetailOpen.value = false
  }
)

/** 保留浏览器修饰键行为，普通点击复用 OA 和外部来源跳转。 */
const openSource = (event) => {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
  event.preventDefault()
  navigateSource({ url: props.citation.url, sourceType: props.citation.source_type })
}
</script>

<style scoped lang="less">
.answer-citation-detail {
  color: var(--gray-900);
  overflow-wrap: anywhere;
}
.citation-excerpt {
  margin-bottom: 20px;
  .excerpt-label {
    color: var(--gray-600);
    font-size: 12px;
  }
  blockquote {
    margin: 0;
    padding: 10px 14px;
    border-left: 3px solid var(--gray-200);
    background: var(--gray-25);
    white-space: pre-wrap;
    line-height: 1.75;
  }
}
.citation-missing {
  color: var(--gray-600);
}
.citation-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  a {
    color: var(--main-700);
  }
}
</style>
