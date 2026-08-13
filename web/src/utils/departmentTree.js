/**
 * 将组织节点平铺列表组装为 Ant Design 表格与树选择器可直接使用的树。
 */
export function buildDepartmentTree(departments, disabledRootId = null) {
  const nodes = new Map(
    departments.map((department) => [
      department.id,
      { ...department, children: [] }
    ])
  )
  const roots = []

  for (const node of nodes.values()) {
    const parent = nodes.get(node.parent_id)
    if (parent) parent.children.push(node)
    else roots.push(node)
  }

  const pending = disabledRootId == null ? [] : [nodes.get(disabledRootId)]
  while (pending.length) {
    const node = pending.pop()
    if (!node || node.disabled) continue
    node.disabled = true
    pending.push(...node.children)
  }

  for (const node of nodes.values()) {
    if (!node.children.length) delete node.children
  }

  return roots
}
