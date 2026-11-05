<template>
  <div class="spreadsheet-preview">
    <div class="sheet-toolbar">
      <label>
        工作表
        <select v-model="selected" aria-label="工作表" :disabled="!sheets.length">
          <option v-for="(item, index) in sheets" :key="index" :value="index">
            {{ item.name }}
          </option>
        </select>
      </label>
      <span>只读 · 公式显示文件中保存的结果</span>
    </div>
    <div v-if="!sheet?.rows?.length" class="sheet-empty">当前没有可显示的单元格</div>
    <div v-else class="sheet-scroll" tabindex="0" :aria-label="`${sheet.name}，可滚动表格`">
      <table class="sheet-grid">
        <caption>
          {{
            sheet.name
          }}
        </caption>
        <thead>
          <tr>
            <th scope="col" aria-label="行号"></th>
            <th v-for="(_, index) in sheet.rows[0]" :key="index" scope="col">
              {{ columnName(index) }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, rowIndex) in sheet.rows" :key="rowIndex">
            <th scope="row">{{ rowIndex + 1 }}</th>
            <template v-for="(cell, colIndex) in row" :key="colIndex">
              <td
                v-if="cell"
                :rowspan="cell.rowspan || 1"
                :colspan="cell.colspan || 1"
                :style="cell.style"
                :title="
                  cell.format && cell.format !== 'General' ? `原单元格格式：${cell.format}` : ''
                "
              >
                {{ cell.text }}
              </td>
            </template>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({ workbook: { type: Object, default: null } })
const selected = ref(0)
const sheets = computed(() => props.workbook?.sheets || [])
const sheet = computed(() => sheets.value[selected.value])
watch(
  () => props.workbook,
  () => {
    selected.value = 0
  }
)

/** 将零基列号转换为 Excel 列名。 */
const columnName = (index) => {
  let name = ''
  for (let value = index + 1; value > 0; value = Math.floor((value - 1) / 26)) {
    name = String.fromCharCode(65 + ((value - 1) % 26)) + name
  }
  return name
}
</script>

<style scoped lang="less">
.spreadsheet-preview {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 180px;
  min-width: 0;
  color: var(--color-text);
}
.sheet-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  padding: 8px;
  background: var(--gray-10);
  font-size: 13px;
  span {
    color: var(--color-text-secondary);
  }
  select {
    max-width: 220px;
    min-height: 36px;
    margin-left: 8px;
    border: 1px solid var(--gray-200);
    border-radius: 4px;
    background: var(--gray-0);
    color: var(--color-text);
  }
}
.sheet-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  // 文档表面保持浅色，保留源文件文字与填充色之间的对比度。
  color-scheme: light;
  background: Canvas;
  color: CanvasText;
}
select:focus-visible,
.sheet-scroll:focus-visible {
  outline: 2px solid var(--main-color);
  outline-offset: -2px;
}
.sheet-grid {
  border-collapse: separate;
  border-spacing: 0;
  font-size: 13px;
  caption {
    text-align: left;
    padding: 8px;
  }
  td,
  th {
    min-width: 96px;
    padding: 6px 8px;
    border-right: 1px solid ButtonBorder;
    border-bottom: 1px solid ButtonBorder;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  th {
    background: ButtonFace;
    color: ButtonText;
    font-weight: normal;
  }
  thead th {
    position: sticky;
    top: 0;
    z-index: 1;
  }
  tbody th,
  thead th:first-child {
    position: sticky;
    left: 0;
    min-width: 40px;
  }
  thead th:first-child {
    z-index: 2;
  }
}
.sheet-empty {
  padding: 24px;
  color: var(--color-text-secondary);
}
</style>
