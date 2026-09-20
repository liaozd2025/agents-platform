export const AUTO_PROJECT_ID = '__auto__'

// 服务端托管的会话目录名就是 Project 的 uuid（implicit Project 没有名字），
// 直接展示 uuid 会让用户以为是乱码，因此前端统一兜底成可读文案。
const MANAGED_PROJECT_DIR_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
export const UNNAMED_PROJECT_DIR_LABEL = '历史会话目录'

// 目录条目路径形如 `/projects/<uuid>/`（前后都带斜杠），
// Project 与会话侧的 workdir_path 形如 `projects/<uuid>`（不带斜杠），统一归一成同一键。
export const toDirectoryKey = (path) => {
  const normalized = String(path || '').replace(/^\/+|\/+$/g, '')
  return normalized ? `/${normalized}/` : ''
}

// 仅 `/projects/<uuid>/` 这一层是托管会话目录；更深层级（用户自建子目录）语义明确，不做替换。
export const isManagedProjectDirectory = (path) => {
  const parts = toDirectoryKey(path).split('/').filter(Boolean)
  return parts.length === 2 && parts[0] === 'projects' && MANAGED_PROJECT_DIR_PATTERN.test(parts[1])
}

/**
 * 生成托管会话目录的展示名映射（键为目录条目路径，如 `/projects/<uuid>/`）。
 * 优先级：已命名 Project 的名称 > 最近会话标题 > 兜底文案。
 */
export const buildDirectoryLabels = ({ projects = [], historyCandidates = [] } = {}) => {
  const labels = {}
  // 先落会话标题，再让项目名覆盖：已经起过名字的项目比会话标题更可辨识。
  for (const candidate of historyCandidates) {
    const key = toDirectoryKey(candidate?.workdir_path)
    if (!isManagedProjectDirectory(key)) continue
    const title = String(candidate?.title || '').trim()
    if (title) labels[key] = title
  }
  for (const project of projects) {
    const key = toDirectoryKey(project?.workdir_path)
    if (!isManagedProjectDirectory(key)) continue
    const name = String(project?.name || '').trim()
    if (name) labels[key] = name
  }
  return labels
}

/**
 * 解析单个目录的展示信息，供目录选择器渲染。
 * 返回 null 表示该目录保持服务端原始名称。
 */
export const resolveDirectoryLabel = (path, labels = {}) => {
  const key = toDirectoryKey(path)
  if (!isManagedProjectDirectory(key)) return null
  return { label: labels[key] || UNNAMED_PROJECT_DIR_LABEL, hint: key }
}

export const filterProjects = (projects, query = '') => {
  const keyword = String(query).trim().toLocaleLowerCase()
  if (!keyword) return projects
  return projects.filter((project) => project.name.toLocaleLowerCase().includes(keyword))
}

export const formatRelativeTime = (value, now = Date.now()) => {
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return ''

  const elapsed = Math.max(0, Number(now) - timestamp)
  const minute = 60 * 1000
  const hour = 60 * minute
  const day = 24 * hour
  if (elapsed < minute) return '刚刚'
  if (elapsed < hour) return `${Math.floor(elapsed / minute)}分钟前`
  if (elapsed < day) return `${Math.floor(elapsed / hour)}小时前`
  if (elapsed < 30 * day) return `${Math.floor(elapsed / day)}天前`
  if (elapsed < 365 * day) return `${Math.floor(elapsed / (30 * day))}个月前`
  return `${Math.floor(elapsed / (365 * day))}年前`
}
