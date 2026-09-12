<template>
  <transition name="chunk-detail-slide">
    <section
      v-if="open"
      class="chunk-detail-overlay"
      role="dialog"
      aria-modal="true"
      :aria-label="modalTitle"
    >
      <header class="chunk-detail-header">
        <button
          type="button"
          class="back-btn"
          title="返回"
          aria-label="返回查看详情"
          @click="close"
        >
          <ArrowLeft :size="18" />
        </button>
        <h3 class="chunk-detail-title" :title="modalTitle">{{ modalTitle }}</h3>
      </header>

      <div class="chunk-detail-body">
        <div v-if="chunk" class="detail-meta">
          <span v-if="typeof chunk.score === 'number'" class="score"
            >相似度 {{ (chunk.score * 100).toFixed(1) }}%</span
          >
          <span v-if="chunk.metadata?.chunk_id" class="meta-item"
            >chunk_id: {{ chunk.metadata.chunk_id }}</span
          >
          <span v-if="lineRange" class="meta-item">{{ lineRange }}</span>
        </div>

        <MarkdownPreview
          v-if="chunk?.content"
          :content="chunk.content"
          class="chunk-markdown-content"
        />
        <div v-else class="empty-text">暂无内容</div>
      </div>
    </section>
  </transition>
</template>

<script setup>
import { computed } from 'vue'
import { ArrowLeft } from '@lucide/vue'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'

const props = defineProps({
  open: {
    type: Boolean,
    default: false
  },
  chunk: {
    type: Object,
    default: null
  },
  titlePrefix: {
    type: String,
    default: '文档片段详情'
  }
})

const emit = defineEmits(['update:open'])

/** 关闭分片详情，回退到原来的查看详情弹窗 */
const close = () => {
  emit('update:open', false)
}

const modalTitle = computed(() => {
  const source = props.chunk?.metadata?.source
  return source ? `${props.titlePrefix} - ${source}` : props.titlePrefix
})

const lineRange = computed(() => {
  const startLine = Number(props.chunk?.metadata?.start_line || 0)
  const endLine = Number(props.chunk?.metadata?.end_line || 0)
  if (!startLine || !endLine) return ''
  return startLine === endLine ? `第 ${startLine} 行` : `第 ${startLine}-${endLine} 行`
})
</script>

<style scoped lang="less">
/* 只覆盖父级查看详情弹窗的窗口：绝对定位在弹窗内容区之上，关闭时整层移除 */
.chunk-detail-overlay {
  position: absolute;
  inset: 0;
  z-index: 1100;
  display: flex;
  flex-direction: column;
  border-radius: 8px;
  background: var(--gray-0);
}

.chunk-detail-header {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: 10px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--gray-150);
  background: var(--gray-0);
}

.chunk-detail-title {
  margin: 0;
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--gray-900);
  font-size: 15px;
  font-weight: 600;
  line-height: 22px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.back-btn {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  padding: 0;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--gray-700);
  cursor: pointer;
  transition:
    background-color 0.15s ease,
    color 0.15s ease;

  &:hover {
    background: var(--gray-150);
    color: var(--gray-900);
  }

  &:focus-visible {
    outline: 2px solid var(--main-400);
    outline-offset: 2px;
  }
}

.chunk-detail-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 20px 24px 24px;
}

.detail-meta {
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--gray-600);
  display: flex;
  flex-wrap: wrap;
  gap: 10px;

  .score {
    color: var(--gray-700);
    font-weight: 600;
  }

  .meta-item {
    color: var(--gray-600);
  }
}

.empty-text {
  color: var(--gray-500);
  font-size: 13px;
}

.chunk-detail-slide-enter-active,
.chunk-detail-slide-leave-active {
  transition:
    transform 0.22s ease,
    opacity 0.22s ease;
}

.chunk-detail-slide-enter-from,
.chunk-detail-slide-leave-to {
  transform: translateX(32px);
  opacity: 0;
}

@media (prefers-reduced-motion: reduce) {
  .chunk-detail-slide-enter-active,
  .chunk-detail-slide-leave-active {
    transition: none;
  }
}
</style>

<style lang="less">
/* 覆盖层的包含块必须是父级弹窗内容区；antd 默认已是 relative，这里兜底，避免退化成整页覆盖 */
.ant-modal-content:has(.chunk-detail-overlay) {
  position: relative;
}
</style>
