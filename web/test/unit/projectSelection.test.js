import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  UNNAMED_PROJECT_DIR_LABEL,
  buildDirectoryLabels,
  filterProjects,
  formatRelativeTime,
  resolveDirectoryLabel
} from '../../src/utils/projectSelection.js'

test('Project 搜索按名称过滤且无匹配时返回空列表', () => {
  const projects = [{ name: 'Desktop' }, { name: '论文写作' }, { name: 'Agent Skills' }]

  assert.deepEqual(filterProjects(projects, '  agent  '), [{ name: 'Agent Skills' }])
  assert.deepEqual(filterProjects(projects, '不存在'), [])
  assert.equal(filterProjects(projects, ''), projects)
})

test('历史项目时间按分钟到年份显示相对时间', () => {
  const now = Date.parse('2026-08-22T12:00:00Z')

  assert.equal(formatRelativeTime('2026-08-22T11:59:30Z', now), '刚刚')
  assert.equal(formatRelativeTime('2026-08-22T11:55:00Z', now), '5分钟前')
  assert.equal(formatRelativeTime('2026-08-22T09:00:00Z', now), '3小时前')
  assert.equal(formatRelativeTime('2026-08-19T12:00:00Z', now), '3天前')
  assert.equal(formatRelativeTime('2026-06-22T12:00:00Z', now), '2个月前')
  assert.equal(formatRelativeTime('2024-08-22T12:00:00Z', now), '2年前')
  assert.equal(formatRelativeTime('invalid', now), '')
})

const UUID_A = '548f7838-d284-4d5e-b977-188f55cca0d4'
const UUID_B = 'bcb7bd60-ef65-4261-a879-367267032f66'

test('托管会话目录优先展示项目名，其次会话标题', () => {
  const labels = buildDirectoryLabels({
    projects: [{ name: '产品发布计划', workdir_path: `projects/${UUID_A}` }],
    historyCandidates: [
      { title: '整理销售表', workdir_path: `projects/${UUID_A}` },
      { title: '周报草稿', workdir_path: `projects/${UUID_B}` }
    ]
  })

  // 已命名的项目覆盖同一目录的会话标题
  assert.equal(resolveDirectoryLabel(`/projects/${UUID_A}/`, labels).label, '产品发布计划')
  assert.equal(resolveDirectoryLabel(`/projects/${UUID_B}/`, labels).label, '周报草稿')
})

test('无名称来源的托管目录兜底为历史会话目录而不是裸 uuid', () => {
  const labels = buildDirectoryLabels({
    projects: [{ name: null, workdir_path: `projects/${UUID_B}` }],
    historyCandidates: [{ title: null, workdir_path: `projects/${UUID_B}` }]
  })

  assert.equal(resolveDirectoryLabel(`/projects/${UUID_B}/`, labels).label, UNNAMED_PROJECT_DIR_LABEL)
})

test('仅 projects 下一层 uuid 目录被改名，其余路径保持原样', () => {
  const labels = buildDirectoryLabels({
    projects: [{ name: '桌面项目', workdir_path: 'Desktop' }],
    historyCandidates: [{ title: '嵌套目录会话', workdir_path: `projects/${UUID_A}/reports` }]
  })

  assert.equal(resolveDirectoryLabel('/Desktop/', labels), null)
  assert.equal(resolveDirectoryLabel('/notes/', labels), null)
  assert.equal(resolveDirectoryLabel(`/projects/${UUID_A}/reports/`, labels), null)
  // projects 根目录本身不参与改名
  assert.equal(resolveDirectoryLabel('/projects/', labels), null)
})

const readSource = (relativePath) => readFileSync(new URL(relativePath, import.meta.url), 'utf8')

test('目录选择器按展示名渲染可读名称并保留原始目录名', () => {
  const source = readSource('../../src/components/WorkspacePathPicker.vue')

  assert.match(source, /resolveDirectoryLabel: \{ type: Function, default: null \}/)
  assert.match(source, /entry\.label\.label/)
  assert.match(source, /entry\.label\.hint/)
})

test('新建项目弹窗向目录选择器注入名称解析器', () => {
  const source = readSource('../../src/components/ProjectSelectionSection.vue')

  assert.match(source, /:resolve-directory-label="projectsStore\.resolveDirectoryLabel"/)
  assert.match(source, /void projectsStore\.ensureDirectoryLabels\(\)/)
})

test('上传与产物保存的目录选择器注入同一名称解析器', () => {
  const upload = readSource('../../src/components/FileUploadModal.vue')
  const artifacts = readSource('../../src/components/AgentArtifactsCard.vue')
  const store = readSource('../../src/stores/projects.js')

  assert.match(upload, /:resolve-directory-label="projectsStore\.resolveDirectoryLabel"/)
  assert.match(upload, /void projectsStore\.ensureDirectoryLabels\(\)/)
  assert.match(artifacts, /:resolve-directory-label="projectsStore\.resolveDirectoryLabel"/)
  assert.match(artifacts, /void projectsStore\.ensureDirectoryLabels\(\)/)
  assert.match(store, /ensureDirectoryLabels/)
})
