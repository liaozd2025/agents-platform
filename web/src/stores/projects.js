import { ref } from 'vue'
import { defineStore } from 'pinia'
import { projectApi } from '@/apis/project_api'
// 托管会话目录名就是 uuid，各入口共用同一份「目录 → 项目名/会话标题」映射来展示可读名称。
import { buildDirectoryLabels, resolveDirectoryLabel as resolveProjectDirectoryLabel } from '@/utils/projectSelection'

export const useProjectsStore = defineStore('projects', () => {
  const projects = ref([])
  const isLoading = ref(false)
  const error = ref('')
  let requestVersion = 0

  // 目录名称映射与进行中的请求：多个入口共用，避免各自重复拉取
  const directoryLabels = ref({})
  let directoryLabelsPromise = null

  /** 拉取项目与会话标题生成目录名称映射；失败保留空映射，调用方回退为原始目录名。 */
  const loadDirectoryLabels = async () => {
    try {
      const [list, historyCandidates] = await Promise.all([
        projectApi.getProjects(),
        projectApi.getHistoryCandidates({ limit: 100 })
      ])
      directoryLabels.value = buildDirectoryLabels({
        projects: Array.isArray(list) ? list : [],
        historyCandidates: Array.isArray(historyCandidates?.items) ? historyCandidates.items : []
      })
    } catch (loadError) {
      console.warn('加载项目目录名称失败，将显示原始目录名', loadError)
    }
    return directoryLabels.value
  }

  const ensureDirectoryLabels = () => {
    if (!directoryLabelsPromise) directoryLabelsPromise = loadDirectoryLabels()
    return directoryLabelsPromise
  }

  /** 解析单个路径的展示名；返回 null 表示该路径沿用原始名称。 */
  const resolveDirectoryLabel = (path) => resolveProjectDirectoryLabel(path, directoryLabels.value)

  const invalidatePendingLoad = () => {
    requestVersion += 1
    isLoading.value = false
    error.value = ''
  }

  const loadProjects = async () => {
    const currentVersion = ++requestVersion
    isLoading.value = true
    error.value = ''
    try {
      const loadedProjects = (await projectApi.getProjects()) || []
      if (currentVersion === requestVersion) projects.value = loadedProjects
      return projects.value
    } catch (loadError) {
      if (currentVersion === requestVersion) error.value = '项目加载失败'
      throw loadError
    } finally {
      if (currentVersion === requestVersion) isLoading.value = false
    }
  }

  const upsertProject = (project) => {
    if (!project?.id) return
    invalidatePendingLoad()
    projects.value = [project, ...projects.value.filter((item) => item.id !== project.id)]
  }

  const replaceProject = (project) => {
    if (!project?.id) return
    invalidatePendingLoad()
    projects.value = projects.value.map((item) => (item.id === project.id ? project : item))
  }

  const removeProject = (projectId) => {
    if (!projectId) return
    invalidatePendingLoad()
    projects.value = projects.value.filter((project) => project.id !== projectId)
  }

  const reset = () => {
    invalidatePendingLoad()
    projects.value = []
  }

  return {
    projects,
    isLoading,
    error,
    loadProjects,
    upsertProject,
    replaceProject,
    removeProject,
    reset,
    directoryLabels,
    ensureDirectoryLabels,
    resolveDirectoryLabel
  }
})
