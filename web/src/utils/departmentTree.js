/**
 * 将组织节点平铺列表组装为 Ant Design 表格与树选择器可直接使用的树。
 */
export function buildDepartmentTree(departments, disabledRootId = null) {
  const nodes = new Map(
    departments.map((department) => [department.id, { ...department, children: [] }])
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

/**
 * 返回拥有子节点的组织节点 ID，用于树表格全部展开。
 */
export function getDepartmentExpandableKeys(departments) {
  const departmentIds = new Set(departments.map((department) => Number(department.id)))
  return [
    ...new Set(
      departments
        .map((department) => Number(department.parent_id))
        .filter((parentId) => departmentIds.has(parentId))
    )
  ]
}

/**
 * 移除已被选中上级覆盖的后代节点。
 */
export function normalizeDepartmentSelection(departments, selectedIds) {
  const departmentById = new Map(departments.map((item) => [Number(item.id), item]))
  const selected = new Set(selectedIds.map(Number).filter(Number.isFinite))

  return [...selected].filter((departmentId) => {
    let parentId = departmentById.get(departmentId)?.parent_id
    while (parentId != null) {
      if (selected.has(Number(parentId))) return false
      parentId = departmentById.get(Number(parentId))?.parent_id
    }
    return true
  })
}

/**
 * 判断每个管理节点是否位于任一读取节点的子树内。
 */
export function isDepartmentSelectionCovered(departments, readIds, manageIds) {
  const departmentById = new Map(departments.map((item) => [Number(item.id), item]))
  const read = new Set(readIds.map(Number))

  return manageIds.every((departmentId) => {
    let currentId = Number(departmentId)
    while (Number.isFinite(currentId)) {
      if (read.has(currentId)) return true
      const parentId = departmentById.get(currentId)?.parent_id
      if (parentId == null) return false
      currentId = Number(parentId)
    }
    return false
  })
}

/**
 * 生成能直接表达组织子树覆盖范围的摘要。
 */
export function getDepartmentSelectionSummary(departments, selectedIds) {
  const departmentById = new Map(departments.map((item) => [Number(item.id), item]))
  const names = selectedIds.map(
    (departmentId) => departmentById.get(Number(departmentId))?.name || `组织节点 ${departmentId}`
  )

  if (!names.length) return '未选择组织节点'
  if (names.length === 1) return `${names[0]}及其下级组织`
  return `${names.slice(0, 2).join('、')}等 ${names.length} 个组织子树`
}

/**
 * 构建共享范围选择树，禁用已由上级节点覆盖的后代。
 */
export function buildDepartmentShareTree(departments, selectedIds, searchQuery = '') {
  const selected = new Set(normalizeDepartmentSelection(departments, selectedIds))

  const decorate = (node, inherited = false) => {
    const currentSelected = selected.has(Number(node.id))
    const result = {
      ...node,
      key: Number(node.id),
      title: node.name
    }
    if (inherited) {
      result.disabled = true
      result.inherited = true
    }
    if (node.children?.length) {
      result.children = node.children.map((child) => decorate(child, inherited || currentSelected))
    }
    return result
  }

  const tree = buildDepartmentTree(departments).map((node) => decorate(node))
  const query = searchQuery.trim().toLowerCase()
  if (!query) return tree

  const filter = (node) => {
    if (String(node.title).toLowerCase().includes(query)) return node

    const children = (node.children || []).map(filter).filter(Boolean)
    return children.length ? { ...node, children } : null
  }
  return tree.map(filter).filter(Boolean)
}
