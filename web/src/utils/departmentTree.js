/**
 * 将组织节点平铺列表组装为 Ant Design 表格与树选择器可直接使用的树。
 */
export function buildDepartmentTree(departments, disabledIds = new Set()) {
  const nodes = new Map(
    departments.map((department) => [
      department.id,
      { ...department, ...(disabledIds.has(department.id) && { disabled: true }), children: [] }
    ])
  )
  const roots = []

  for (const node of nodes.values()) {
    const parent = nodes.get(node.parent_id)
    if (parent) parent.children.push(node)
    else roots.push(node)
  }

  for (const node of nodes.values()) {
    if (!node.children.length) delete node.children
  }

  return roots
}

/**
 * 返回指定组织节点自身及全部后代 ID，供移动父节点选择器禁用非法目标。
 */
export function getDepartmentSubtreeIds(departments, departmentId) {
  const childrenByParent = new Map()

  for (const department of departments) {
    const children = childrenByParent.get(department.parent_id) || []
    children.push(department.id)
    childrenByParent.set(department.parent_id, children)
  }

  const subtreeIds = new Set()
  const pending = [departmentId]

  while (pending.length) {
    const currentId = pending.pop()
    if (subtreeIds.has(currentId)) continue
    subtreeIds.add(currentId)
    pending.push(...(childrenByParent.get(currentId) || []))
  }

  return subtreeIds
}
