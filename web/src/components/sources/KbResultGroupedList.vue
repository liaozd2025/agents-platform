<template>
  <div class="kb-result-grouped-list">
    <div v-if="showSummary" class="result-summary">
      找到 {{ normalizedChunks.length }} 个相关文档片段，来自 {{ fileGroupList.length }} 个文件
    </div>

    <div class="kb-results" v-if="normalizedChunks.length > 0">
      <div v-for="(fileGroup, index) in fileGroupList" :key="fileGroup.key" class="file-group-item">
        <a
          v-if="fileGroup.url"
          class="file-info"
          :href="fileGroup.url"
          target="_blank"
          rel="noopener noreferrer"
          :aria-label="`打开 ${fileGroup.filename} 的 OA 原文`"
        >
          <span class="source-index">{{ index + 1 }}</span>
          <span class="file-copy">
            <span class="file-name" :title="fileGroup.filename">{{ fileGroup.filename }}</span>
            <span v-if="fileGroup.author" class="file-author">知识库 · 作者：{{ fileGroup.author }}</span>
            <span v-else class="file-author">知识库</span>
          </span>
          <ExternalLink :size="14" class="external-icon" />
        </a>
        <button v-else class="file-info" :aria-label="`查看 ${fileGroup.filename} 的检索片段`" @click="openFileChunksModal(fileGroup)">
          <span class="source-index">{{ index + 1 }}</span>
          <span class="file-copy">
            <span class="file-name" :title="fileGroup.filename">{{ fileGroup.filename }}</span>
            <span class="file-author">知识库 · {{ fileGroup.chunks.length }} 个片段</span>
          </span>
        </button>
        <button v-if="fileGroup.kb_id && fileGroup.file_id" class="view-file-btn" title="查看完整文件" aria-label="查看完整文件" @click="openFileDetail(fileGroup)">
          <Eye :size="14" />
        </button>
      </div>
    </div>

    <div v-else class="no-results">
      <p>{{ emptyText }}</p>
    </div>
    <KbFileChunksModal v-model:open="chunksModalVisible" :file-group="selectedFileGroup" />
    <FileDetailModal v-model:open="fileDetailOpen" :kb-id="fileDetailKbId" :file-id="fileDetailFileId" />
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ExternalLink, Eye } from '@lucide/vue'
import KbFileChunksModal from './KbFileChunksModal.vue'
import FileDetailModal from '@/components/FileDetailModal.vue'
import { groupKnowledgeChunks } from '@/utils/kbResultGroups.js'

const props = defineProps({
  chunks: {
    type: [Array, Object],
    default: () => []
  },
  showSummary: {
    type: Boolean,
    default: true
  },
  emptyText: {
    type: String,
    default: '未找到相关知识库内容'
  }
})

const chunksModalVisible = ref(false)
const selectedFileGroup = ref(null)
const fileDetailOpen = ref(false)
const fileDetailKbId = ref('')
const fileDetailFileId = ref('')

const openFileChunksModal = (fileGroup) => {
  selectedFileGroup.value = fileGroup
  chunksModalVisible.value = true
}
const openFileDetail = (fileGroup) => {
  fileDetailKbId.value = fileGroup.kb_id
  fileDetailFileId.value = fileGroup.file_id
  fileDetailOpen.value = true
}

const resolveChunks = (input) => {
  if (Array.isArray(input)) return input
  if (!input || typeof input !== 'object') return []

  if (Array.isArray(input.chunks)) return input.chunks
  if (Array.isArray(input.data?.chunks)) return input.data.chunks

  return []
}

const normalizedChunks = computed(() =>
  resolveChunks(props.chunks)
    .filter((item) => item && typeof item === 'object' && item.content)
    .map((item) => {
      const metadata = item.metadata && typeof item.metadata === 'object' ? item.metadata : {}
      const source =
        metadata.source ||
        metadata.file_name ||
        metadata.filename ||
        metadata.title ||
        item.file_name ||
        item.filename ||
        item.file_id ||
        item.kb_id ||
        '未知来源'

      return {
        ...item,
        score: typeof item.score === 'number' ? item.score : metadata.score,
        rerank_score:
          typeof item.rerank_score === 'number' ? item.rerank_score : metadata.rerank_score,
        metadata: {
          ...metadata,
          source,
          chunk_id: metadata.chunk_id || item.id
        }
      }
    })
)

const fileGroupList = computed(() => {
  return groupKnowledgeChunks(normalizedChunks.value)
})

</script>

<style scoped lang="less">
.kb-result-grouped-list {
  padding: 4px;
  .result-summary {
    padding: 6px 10px;
    background: var(--gray-25);
    font-size: 12px;
    color: var(--gray-700);
    border: 1px solid var(--gray-150);
    border-radius: 8px;
    margin-bottom: 6px;
  }

  .view-file-btn {
    flex-shrink: 0;
    background: transparent;
    border: none;
    color: var(--gray-600);
    cursor: pointer;
    padding: 6px;
  }

  .kb-results {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .file-group-item {
    border: 1px solid var(--gray-150);
    border-radius: 8px;
    background: var(--gray-0);
    padding: 6px 10px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    cursor: pointer;
    transition: all 0.15s ease;

    &:hover {
      background: var(--gray-25);
      border-color: var(--gray-200);
    }

    .file-info {
      display: flex;
      align-items: center;
      gap: 8px;
      flex: 1;
      min-width: 0;
      padding: 0;
      border: 0;
      background: transparent;
      text-align: left;
      cursor: pointer;

      &:focus-visible {
        outline: 2px solid var(--main-400);
        outline-offset: 2px;
        border-radius: 4px;
      }

      .source-index {
        flex-shrink: 0;
        width: 20px;
        color: var(--gray-600);
        font-size: 13px;
        text-align: center;
      }

      .file-name {
        font-size: 13px;
        color: var(--gray-800);
        font-weight: 500;
        min-width: 0;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .file-copy {
        display: flex;
        flex: 1;
        flex-direction: column;
        gap: 1px;
        min-width: 0;
      }

      .file-author {
        overflow: hidden;
        color: var(--gray-500);
        font-size: 11px;
        line-height: 15px;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .external-icon { flex-shrink: 0; color: var(--gray-500); }
    }
  }

  .no-results {
    text-align: center;
    color: var(--gray-700);
    padding: 10px;
    font-size: 12px;
    border: 1px dashed var(--gray-200);
    border-radius: 8px;
  }
}
</style>
