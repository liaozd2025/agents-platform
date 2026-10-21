<template>
  <a-modal
    :open="open"
    title="产品事实表（Fact Layer）"
    :width="1100"
    :footer="null"
    destroy-on-close
    @cancel="emit('close')"
  >
    <div class="fact-layer-toolbar">
      <a-input-search
        v-model:value="productFilter"
        placeholder="按产品名过滤"
        style="width: 240px"
        allow-clear
        @search="loadRows"
      />
      <div class="fact-layer-actions">
        <span class="fact-layer-total">
          共 {{ rows.length }} 个产品{{ editMode ? ' · 点击单元格编辑' : '' }}{{ deleteMode ? ' · 勾选要删除的产品' : '' }}
        </span>
        <a-button size="small" @click="openAddModal">新增</a-button>
        <a-button
          v-if="!deleteMode"
          size="small"
          danger
          @click="enterDeleteMode"
        >
          删除
        </a-button>
        <a-button
          v-else
          size="small"
          :class="selectedRowKeys.length ? 'confirm-delete' : 'cancel-delete'"
          :loading="deleting"
          @click="onDeleteButtonClick"
        >
          {{ selectedRowKeys.length ? `确认删除(${selectedRowKeys.length})` : '取消删除' }}
        </a-button>
        <a-button
          size="small"
          :class="{ 'edit-mode-on': editMode }"
          @click="toggleEditMode"
        >
          {{ editMode ? '退出编辑' : '修改' }}
        </a-button>
      </div>
    </div>
    <a-table
      :columns="tableColumns"
      :data-source="rows"
      :loading="loading"
      :pagination="{ pageSize: 20, showSizeChanger: false }"
      row-key="product_id"
      size="small"
      :scroll="{ x: scrollX, y: 480 }"
      :row-selection="deleteMode ? rowSelection : undefined"
      :custom-row="customRow"
      :row-class-name="rowClassName"
    >
      <template #bodyCell="{ column, record, text }">
        <template v-if="column.key === 'product_id'">
          <div class="fact-value-cell">{{ text }}</div>
        </template>
        <template v-else-if="isEditing(record, column)">
          <div class="fact-edit-box">
            <a-textarea
              v-model:value="editDraft"
              placeholder="输入新值，清空表示删除该值"
              class="fact-edit-textarea"
            />
            <div class="fact-edit-actions">
              <a-button size="small" type="primary" :loading="saving" @click="saveEdit">保存</a-button>
              <a-button size="small" @click="cancelEdit">取消</a-button>
            </div>
          </div>
        </template>
        <template v-else>
          <div
            :class="['fact-value-cell', { 'fact-editable': editMode }]"
            :title="editMode ? '点击编辑' : ''"
            @click="editMode && startEdit(record, column)"
          >
            {{ text || '-' }}
          </div>
        </template>
      </template>
    </a-table>

    <a-modal
      :open="addOpen"
      title="新增产品"
      :width="420"
      ok-text="确定"
      cancel-text="取消"
      :confirm-loading="adding"
      @ok="submitAdd"
      @cancel="addOpen = false"
    >
      <a-input
        v-model:value="addName"
        placeholder="输入产品名称"
        :maxlength="255"
        @keydown.enter.prevent="submitAdd"
      />
    </a-modal>
  </a-modal>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { databaseApi } from '@/apis/knowledge_api'

const props = defineProps({
  open: { type: Boolean, default: false },
  kbId: { type: String, default: '' }
})

const emit = defineEmits(['close'])

const loading = ref(false)
const rows = ref([])
const factColumns = ref([])
const productFilter = ref('')

// 产品列不固定，随内容横向滚动
const tableColumns = computed(() => [
  { title: '产品', dataIndex: 'product_id', key: 'product_id', width: 150 },
  ...factColumns.value.map((name) => ({
    title: name,
    dataIndex: name,
    key: name,
    width: 180
  }))
])

const scrollX = computed(() => 150 + factColumns.value.length * 180)

// ===== 行点击高亮 =====
const selectedKey = ref('')

const customRow = (record) => ({
  onClick: () => {
    selectedKey.value = record.product_id
  }
})

const rowClassName = (record) =>
  record.product_id === selectedKey.value ? 'fact-row-selected' : ''

// ===== 编辑模式（右上角「修改」按钮控制） =====
const editMode = ref(false)
const deleteMode = ref(false)
const editingCell = ref(null) // { product, column }
const editDraft = ref('')
const saving = ref(false)

const toggleEditMode = () => {
  editMode.value = !editMode.value
  if (editMode.value) exitDeleteMode()
  cancelEdit()
  if (!editMode.value) selectedRowKeys.value = []
}

const isEditing = (record, column) =>
  editingCell.value?.product === record.product_id &&
  editingCell.value?.column === column.key

const startEdit = (record, column) => {
  selectedKey.value = record.product_id
  editingCell.value = { product: record.product_id, column: column.key }
  editDraft.value = record[column.dataIndex] || ''
}

const cancelEdit = () => {
  editingCell.value = null
  editDraft.value = ''
}

const saveEdit = async () => {
  if (!editingCell.value || saving.value) return
  const { product, column } = editingCell.value
  saving.value = true
  try {
    await databaseApi.updateFactLayerRow(props.kbId, product, { [column]: editDraft.value })
    const row = rows.value.find((item) => item.product_id === product)
    if (row) row[column] = editDraft.value.trim() || null
    message.success('已保存')
    cancelEdit()
  } catch (error) {
    message.error(error?.message || '保存失败')
  } finally {
    saving.value = false
  }
}

// ===== 删除模式（右上角「删除」按钮独立控制，与编辑模式互斥） =====
const selectedRowKeys = ref([])
const deleting = ref(false)

const rowSelection = computed(() => ({
  selectedRowKeys: selectedRowKeys.value,
  onChange: (keys) => {
    selectedRowKeys.value = keys
  }
}))

const enterDeleteMode = () => {
  editMode.value = false
  cancelEdit()
  selectedRowKeys.value = []
  deleteMode.value = true
}

const exitDeleteMode = () => {
  deleteMode.value = false
  selectedRowKeys.value = []
}

const onDeleteButtonClick = () => {
  // 删除模式下同一个按钮承担两个职责：有勾选 → 确认删除；无勾选 → 退出删除模式
  if (selectedRowKeys.value.length) {
    confirmDelete()
  } else {
    exitDeleteMode()
  }
}

const confirmDelete = () => {
  const targets = [...selectedRowKeys.value]
  if (!targets.length) return
  Modal.confirm({
    title: '确认删除',
    content: `确定删除产品「${targets.join('、')}」吗？该操作不可恢复。`,
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    onOk: async () => {
      deleting.value = true
      try {
        for (const pid of targets) {
          await databaseApi.deleteFactLayerRow(props.kbId, pid)
        }
        message.success('已删除')
        exitDeleteMode()
        await loadRows()
      } catch (error) {
        message.error(error?.message || '删除失败')
      } finally {
        deleting.value = false
      }
    }
  })
}

// ===== 新增 =====
const addOpen = ref(false)
const addName = ref('')
const adding = ref(false)

const openAddModal = () => {
  addName.value = ''
  addOpen.value = true
}

const submitAdd = async () => {
  const name = addName.value.trim()
  if (!name) {
    message.warning('请输入产品名称')
    return
  }
  if (adding.value) return
  adding.value = true
  try {
    await databaseApi.createFactLayerRow(props.kbId, name)
    message.success('已新增')
    addOpen.value = false
    await loadRows()
  } catch (error) {
    message.error(error?.message || '新增失败')
  } finally {
    adding.value = false
  }
}

const loadRows = async () => {
  if (!props.kbId) return
  loading.value = true
  cancelEdit()
  selectedRowKeys.value = []
  try {
    const res = await databaseApi.getFactLayerTable(props.kbId, productFilter.value || '')
    const data = res?.data ?? res
    factColumns.value = data?.columns || []
    rows.value = data?.rows || []
  } catch {
    message.error('读取产品事实表失败')
    factColumns.value = []
    rows.value = []
  } finally {
    loading.value = false
  }
}

watch(
  () => props.open,
  (val) => {
    if (val) {
      productFilter.value = ''
      selectedKey.value = ''
      editMode.value = false
      deleteMode.value = false
      loadRows()
    }
  }
)
</script>

<style scoped>
.fact-layer-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.fact-layer-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.fact-layer-total {
  color: var(--text-tertiary, #999);
  font-size: 12px;
}

/* 编辑模式激活时按钮颜色加深 */
.edit-mode-on {
  background: #1f1f1f !important;
  border-color: #1f1f1f !important;
  color: #fff !important;
}

/* 删除模式：有勾选时按钮红色可确认，无勾选时灰色表示取消 */
.confirm-delete {
  background: #ff4d4f !important;
  border-color: #ff4d4f !important;
  color: #fff !important;
}

.cancel-delete {
  background: #f5f5f5 !important;
  border-color: #d9d9d9 !important;
  color: #666 !important;
}

.fact-value-cell {
  display: block;
  height: 120px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
  line-height: 1.5;
}

.fact-editable {
  cursor: text;
}

.fact-editable:hover {
  background: rgba(0, 0, 0, 0.04);
}

.fact-edit-box {
  height: 120px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.fact-edit-textarea {
  flex: 1;
  resize: none;
  font-size: 12px;
}

.fact-edit-actions {
  display: flex;
  gap: 6px;
}

:deep(.fact-row-selected > td),
:deep(.fact-row-selected:hover > td) {
  background-color: rgba(0, 0, 0, 0.08) !important;
}

:deep(.fact-row-selected) {
  cursor: pointer;
}
</style>
