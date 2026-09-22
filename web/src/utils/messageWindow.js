/** 从最新显示条目向前取窗口，保留完整对话事实供引用、导出和状态判断。 */
export function sliceMessageRows(rows, limit) {
  const visible = []
  let remaining = limit
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const row = rows[index]
    const count = Math.max(1, row.displayItems?.length || 0)
    if (remaining <= 0) return { rows: visible, hasEarlier: true }
    if (count > remaining) {
      visible.unshift({ ...row, displayItems: row.displayItems.slice(-remaining) })
      return { rows: visible, hasEarlier: true }
    }
    visible.unshift(row)
    remaining -= count
  }
  return { rows: visible, hasEarlier: false }
}
