<template>
  <div ref="rootEl" class="graph-canvas-container" :class="{ 'is-dark': themeStore.isDark }">
    <div v-show="graphData.nodes.length > 0" ref="container" class="graph-canvas"></div>
    <div class="slots">
      <div v-if="$slots.top" class="overlay top"><slot name="top" /></div>
      <div class="canvas-content"><slot name="content" /></div>
      <div v-if="graphData.nodes.length > 0" class="graph-stats-wrapper">
        <div v-if="activeStatsPanel" class="floating-panel type-stats-card">
          <div class="panel-header">
            <span class="panel-title">{{
              activeStatsPanel === 'node' ? '实体类型' : '关系类型'
            }}</span>
          </div>
          <div class="panel-body">
            <div class="type-stats-list">
              <div
                v-for="item in activeTypeStats"
                :key="item.name"
                class="type-stats-row"
                :title="`${item.name}: ${item.count}`"
              >
                <span class="type-color" :style="{ backgroundColor: item.color }"></span
                ><span class="type-name">{{ item.name }}</span
                ><span class="type-count">{{ item.count }}</span>
              </div>
            </div>
          </div>
        </div>
        <div class="floating-panel graph-stats-panel">
          <button
            class="stat-item"
            :class="{ active: activeStatsPanel === 'node' }"
            type="button"
            @click="toggleStatsPanel('node')"
          >
            <span class="stat-label">实体</span
            ><span class="stat-value">{{ visibleEntityCount }}</span>
          </button>
          <button
            class="stat-item"
            :class="{ active: activeStatsPanel === 'edge' }"
            type="button"
            @click="toggleStatsPanel('edge')"
          >
            <span class="stat-label">关系</span
            ><span class="stat-value">{{ visibleRelationshipCount }}</span>
          </button>
        </div>
      </div>
      <div v-if="$slots.bottom" class="overlay bottom"><slot name="bottom" /></div>
      <aside v-if="relationshipLegend.length" class="graph-relationship-legend" aria-label="图谱图例">
        <span class="legend-title">节点</span>
        <span v-for="item in nodeLegend" :key="`node-${item.name}`" class="legend-item">
          <i class="legend-node" :style="{ backgroundColor: item.color }"></i>{{ item.name }}
        </span>
        <span class="legend-title">关系</span>
        <span v-for="item in relationshipLegend" :key="item.name" class="legend-item">
          <i :style="{ backgroundColor: item.color }"></i>{{ item.name }}
        </span>
      </aside>
    </div>
  </div>
</template>

<script setup>
import ForceGraph3D from '3d-force-graph'
import * as THREE from 'three'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useThemeStore } from '@/stores/theme'

const props = defineProps({
  graphData: { type: Object, required: true, default: () => ({ nodes: [], edges: [] }) },
  graphInfo: { type: Object, default: () => ({}) },
  labelField: { type: String, default: 'name' },
  autoFit: { type: Boolean, default: true },
  autoResize: { type: Boolean, default: true },
  layoutOptions: { type: Object, default: () => ({}) },
  nodeStyleOptions: { type: Object, default: () => ({}) },
  edgeStyleOptions: { type: Object, default: () => ({}) },
  enableFocusNeighbor: { type: Boolean, default: true },
  sizeByDegree: { type: Boolean, default: true },
  highlightKeywords: { type: Array, default: () => [] }
})
const emit = defineEmits(['ready', 'data-rendered', 'node-click', 'edge-click', 'canvas-click'])
const container = ref(null)
const rootEl = ref(null)
const themeStore = useThemeStore()
const activeStatsPanel = ref('')
let graphInstance = null
let resizeObserver = null
let renderTimer = null
let isMounted = false
let autoFitDone = false
let userInteracted = false
let focusedNodeId = null
let hoveredNodeId = null
let highlightedNodeIds = new Set()
let currentNodes = []
let currentEdges = []
const trackedThreeObjects = new Set()
const CHUNK_NODE_LABEL = 'Chunk'
const CHUNK_NODE_COLOR = '#9aacbf'
const CHUNK_MENTION_EDGE_LABEL = 'MENTIONS'
// 白色画布上的统一关系色；节点继续使用类型色，避免关系颜色与节点语义混淆。
// 白色画布上使用中性深灰，保证关系线清晰且不抢过节点颜色。
const RELATION_COLOR = '#a8b1bc'
const NODE_LABEL_COLORS = [
  // 扩充高亮冷暖色，按实体类型稳定取色，减少知识图谱中重复色块造成的误读。
  '#49dfff',
  '#a78bfa',
  '#4ee6af',
  '#ffcf70',
  '#ff80b8',
  '#77baff',
  '#c5f35b',
  '#ff956f',
  '#6df4e2',
  '#d2a8ff',
  '#f6e58d',
  '#73a8ff'
]
function hashString(value) {
  let hash = 0
  for (let index = 0; index < value.length; index += 1)
    hash = (hash << 5) - hash + value.charCodeAt(index)
  return Math.abs(hash | 0)
}
function getPaletteColor(label, colors) {
  return colors[hashString(String(label)) % colors.length]
}
function getNodeVisualLabel(node) {
  return node?.type || node?.normalized?.type || node?.properties?.label || 'Entity'
}
function getEdgeVisualLabel(edge) {
  return edge?.type || edge?.normalized?.type || 'RELATED_TO'
}
function getNodeColor(node) {
  const visualLabel = getNodeVisualLabel(node)
  const baseColor = visualLabel === CHUNK_NODE_LABEL
    ? CHUNK_NODE_COLOR
    : getPaletteColor(visualLabel, NODE_LABEL_COLORS)
  // 同类节点保留同一主色相；用实体标识生成有限的明暗和饱和度变化，避免图中重复出现完全相同的彩球。
  const entityKey = node?.id ?? node?.name ?? node?.label ?? visualLabel
  const variation = (hashString(String(entityKey)) % 5) - 2
  const color = new THREE.Color(baseColor)
  color.offsetHSL(variation * 0.008, variation * 0.018, variation * 0.045)
  return '#' + color.getHexString()
}
function trackThreeObject(object) {
  trackedThreeObjects.add(object)
  return object
}
function disposeThreeObject(object) {
  object?.traverse?.((child) => {
    child.geometry?.dispose?.()
    const materials = Array.isArray(child.material) ? child.material : [child.material]
    materials.filter(Boolean).forEach((material) => {
      material.map?.dispose?.()
      material.dispose?.()
    })
  })
}
// 交互刷新会重建自定义对象，先释放旧几何体、材质和文字纹理，避免 GPU 资源持续累积。
function disposeTrackedThreeObjects() {
  trackedThreeObjects.forEach(disposeThreeObject)
  trackedThreeObjects.clear()
}
function refreshGraphScene() {
  disposeTrackedThreeObjects()
  graphInstance?.refresh()
}
function getEdgeColor() {
  return RELATION_COLOR
}
function isEntityNode(node) {
  return getNodeVisualLabel(node) !== CHUNK_NODE_LABEL
}
function isEntityRelationEdge(edge) {
  return getEdgeVisualLabel(edge) !== CHUNK_MENTION_EDGE_LABEL
}
function normalizeImpact(value, fallback = 0) {
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback
}
// 节点名称来自知识库内容，插入 3D 引擎提示层前必须转义，避免特殊字符破坏 HTML。
function escapeHtml(value) {
  return String(value ?? '').replace(
    /[&<>"']/g,
    (character) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]
  )
}
function buildTypeStats(items, getName, getColor) {
  const counts = new Map()
  for (const item of items || []) {
    const name = getName(item)
    counts.set(name, (counts.get(name) || 0) + 1)
  }
  return Array.from(counts, ([name, count]) => ({
    name,
    count,
    color: getColor({ type: name })
  })).sort((a, b) => b.count - a.count || a.name.localeCompare(b.name))
}
const visibleEntityNodes = computed(() => (props.graphData?.nodes || []).filter(isEntityNode))
const visibleRelationshipEdges = computed(() =>
  (props.graphData?.edges || []).filter(isEntityRelationEdge)
)
const visibleEntityCount = computed(() => visibleEntityNodes.value.length)
const visibleRelationshipCount = computed(() => visibleRelationshipEdges.value.length)
const nodeTypeStats = computed(() =>
  buildTypeStats(visibleEntityNodes.value, getNodeVisualLabel, getNodeColor)
)
const edgeTypeStats = computed(() =>
  buildTypeStats(visibleRelationshipEdges.value, getEdgeVisualLabel, getEdgeColor)
)
const activeTypeStats = computed(() =>
  activeStatsPanel.value === 'node' ? nodeTypeStats.value : edgeTypeStats.value
)
// 图例只展示高频关系类型，避免把一长串低频类型再次变成画面噪音。
// 关系线在画布中统一使用灰白色，图例必须复用同一颜色，避免图例与实际渲染产生误导。
const relationshipLegend = computed(() =>
  edgeTypeStats.value.slice(0, 6).map((item) => ({ ...item, color: RELATION_COLOR }))
)
// 节点和关系分别展示图例，颜色不再承担两类不同的语义，避免用户误读。
const nodeLegend = computed(() => nodeTypeStats.value.slice(0, 4))
function toggleStatsPanel(type) {
  activeStatsPanel.value = activeStatsPanel.value === type ? '' : type
}
function getCanvasTheme() {
  // 图谱固定使用白色空间背景，关系线和标签改用深灰蓝以保证对比度。
  return { background: '#ffffff', edge: RELATION_COLOR, fog: '#ffffff' }
}
function getCoreNodeIds(nodes) {
  // 默认挑出连接最多的少量实体作为阅读入口；点击任意节点后仍可查看完整一跳关系。
  const coreCount = Math.min(18, Math.max(8, Math.ceil(nodes.length * 0.2)))
  return new Set(
    [...nodes]
      .sort((left, right) => right.degree - left.degree || right.impact - left.impact)
      .slice(0, coreCount)
      .map((node) => node.id)
  )
}
function buildGraphData() {
  // 将后端关系字段转换为 nodes/links，并过滤无效边，避免 3D 引擎添加边时抛错。
  const raw = props.graphData || { nodes: [], edges: [] }
  const degrees = new Map((raw.nodes || []).map((node) => [String(node.id), 0]))
  for (const edge of raw.edges || []) {
    const source = String(edge.source_id)
    const target = String(edge.target_id)
    degrees.set(source, (degrees.get(source) || 0) + 1)
    degrees.set(target, (degrees.get(target) || 0) + 1)
  }
  const nodes = (raw.nodes || []).map((node, index) => {
    const id = String(node.id)
    const degree = degrees.get(id) || 0
    const angle = index * 2.399963229728653
    // 初始坐标保持紧凑，后续由力导向展开；过大的初始半径会让图谱长期散落在视野边缘。
    const radius = 44 + Math.sqrt(index + 1) * 18
    return {
      id,
      label: node[props.labelField] ?? node.name ?? id,
      visualLabel: getNodeVisualLabel(node),
      color: getNodeColor(node),
      degree,
      impact: normalizeImpact(node.citations ?? node.references, degree),
      x: Math.cos(angle) * radius,
      y: Math.sin(angle) * radius,
      z: Math.sin(angle * 0.7) * radius * 0.55,
      original: node
    }
  })
  const nodeIds = new Set(nodes.map((node) => node.id))
  const links = (raw.edges || [])
    .map((edge, index) => ({
      id: edge.id ? String(edge.id) : `edge-${index}`,
      source: String(edge.source_id),
      target: String(edge.target_id),
      label: edge.type ?? '',
      visualLabel: getEdgeVisualLabel(edge),
      color: getEdgeColor(edge),
      original: edge
    }))
    .filter(
      (edge) => edge.source !== edge.target && nodeIds.has(edge.source) && nodeIds.has(edge.target)
    )
  const coreNodeIds = getCoreNodeIds(nodes)
  const coreLinks = new Set(
    links
      .filter((link) => coreNodeIds.has(link.source) || coreNodeIds.has(link.target))
      .map((link) => link.id)
  )
  // 核心节点的相邻节点同样保留，主线不会因另一端被降级而断裂。
  for (const link of links) {
    if (coreLinks.has(link.id)) {
      coreNodeIds.add(link.source)
      coreNodeIds.add(link.target)
    }
  }
  for (const node of nodes) node.isCore = coreNodeIds.has(node.id)
  for (const link of links) link.isCore = coreLinks.has(link.id)
  return { nodes, links }
}
function relatedNodeIds(nodeId) {
  const ids = new Set(nodeId ? [nodeId] : [])
  if (!nodeId) return ids
  for (const edge of currentEdges) {
    if (String(edge.source?.id ?? edge.source) === nodeId)
      ids.add(String(edge.target?.id ?? edge.target))
    if (String(edge.target?.id ?? edge.target) === nodeId)
      ids.add(String(edge.source?.id ?? edge.source))
  }
  return ids
}
function isNodeFocused(node) {
  const focusSet = relatedNodeIds(focusedNodeId || hoveredNodeId)
  return focusSet.size > 0 && focusSet.has(String(node.id))
}
function createTextSprite(text, color, scale = 1, strokeColor = '#d9e0e8') {
  const canvas = document.createElement('canvas')
  const context = canvas.getContext('2d')
  const content = String(text || '').slice(0, 18)
  canvas.width = 512
  canvas.height = 96
  context.clearRect(0, 0, canvas.width, canvas.height)
  // 提高纹理分辨率和字号，保证默认视距下节点名与关系类型仍可辨认。
  context.font = '600 38px sans-serif'
  context.textAlign = 'center'
  context.textBaseline = 'middle'
  context.fillStyle = color
  // 白色画布下使用浅色描边，避免标签边缘发虚或与网格融为一体。
  // 深色字体配合细浅灰描边，避免白底下文字被粗描边冲淡。
  context.strokeStyle = strokeColor
  context.lineWidth = 2
  context.shadowColor = 'rgba(17, 24, 39, 0.22)'
  context.shadowBlur = 3
  context.strokeText(content, canvas.width / 2, canvas.height / 2)
  context.fillText(content, canvas.width / 2, canvas.height / 2)
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(canvas), transparent: true, depthWrite: false })
  )
  sprite.scale.set(48 * scale, 8.2 * scale, 1)
  return trackThreeObject(sprite)
}
function makeLinkObject(link) {
  const activeNodeId = focusedNodeId || hoveredNodeId
  const source = String(link.source?.id ?? link.source)
  const target = String(link.target?.id ?? link.target)
  // 全景只给主线关系加标签；聚焦时则完整显示当前节点的关系语义。
  const isFocusedRelation = activeNodeId && (source === activeNodeId || target === activeNodeId)
  if (!isFocusedRelation && !link.isCore) return null
  // 关系标签使用蓝灰色小字，与节点名称的深色粗体形成层级区分。
  return createTextSprite(link.visualLabel, '#64748b', isFocusedRelation ? 0.82 : 0.7, '#ffffff')
}
function makeParticleObject() {
  // 使用小型平滑光点，保留流动反馈但避免菱形造成大量方点噪声。
  return trackThreeObject(new THREE.Mesh(
    new THREE.SphereGeometry(0.24, 12, 8),
    new THREE.MeshBasicMaterial({
      color: '#1682b8',
      transparent: true,
      opacity: 0.82,
      depthWrite: false,
      depthTest: false
    })
  ))
}
function makeNodeObject(node) {
  const focused = isNodeFocused(node) || highlightedNodeIds.has(String(node.id))
  const foreground = node.isCore || focused
  // 节点尺寸以连接数为主，但设上限，防止高频实体遮住本应清晰可见的关系。
  // 节点保持统一球体；整体收小一档，核心节点仅保留轻微层级，避免遮挡细关系线。
  const radius = foreground
    ? props.sizeByDegree
      ? Math.min(6, 3 + Math.sqrt(node.degree + 1) * 0.56 + Math.min(node.impact, 12) * 0.06)
      : 4.2
    : 2.1
  // 节点统一使用球体，避免正方体、菱形等几何体造成无意义的视觉噪声。
  // 使用低多边形二十面体，让每个面产生自然明暗变化，接近参考图的柔和 3D 质感。
  const geometry = new THREE.IcosahedronGeometry(radius, foreground ? 2 : 1)
  const material = new THREE.MeshStandardMaterial({
    color: node.color,
    // 仅保留低强度同色自发光：提亮暗部，但仍由主光源保留球面的高光与阴影。
    emissive: node.color,
    emissiveIntensity: focused ? 0.1 : foreground ? 0.06 : 0.03,
    metalness: 0.08,
    roughness: 0.52,
    transparent: true,
    opacity: focused ? 1 : foreground ? 0.98 : 0.72,
    ...props.nodeStyleOptions.material
  })
  const sphere = new THREE.Mesh(geometry, material)
  sphere.scale.setScalar(focused ? 1.25 : 1)
  // 聚焦节点才显示轻微光晕；常驻光晕会遮住三维连线。
  const glow = new THREE.Mesh(
    new THREE.SphereGeometry(radius * 1.55, 16, 16),
    new THREE.MeshBasicMaterial({
      color: node.color,
      transparent: true,
      // 光晕只保留为极弱的空间提示，不能遮住节点形状和关系线。
      opacity: focused ? 0.06 : 0,
      blending: THREE.AdditiveBlending,
      depthWrite: false
    })
  )
  const group = new THREE.Group()
  group.add(glow, sphere)
  // 只在用户聚焦时显示实体名称，避免常驻标签把连线切碎。
  if (focused) {
    // 节点名称使用接近黑色的粗体，并以白色描边保证压在连线之上仍清晰。
    const name = createTextSprite(node.label, '#0f172a', 1.12, '#ffffff')
    name.position.set(0, radius + 6, 0)
    group.add(name)
  }
  return trackThreeObject(group)
}
function renderGraph() {
  if (!container.value || !isMounted) return
  const isNewInstance = !graphInstance
  // 复用已有 WebGL 实例，避免数据刷新时反复申请和释放 GPU 资源。
  const data = buildGraphData()
  // 空数据或隐藏容器无需启动引擎，避免 requestAnimationFrame 无限自调用占用资源。
  if (!data.nodes.length || container.value.offsetWidth === 0 || container.value.offsetHeight === 0) {
    return
  }
  // 新数据进入场景前重置一次性取景状态；后续仅在布局稳定后自动适配一次。
  autoFitDone = false
  userInteracted = false
  currentNodes = data.nodes
  currentEdges = data.links
  applyHighlightKeywords()
  const theme = getCanvasTheme()
  // v-show 切换后首帧可能仍是隐藏状态，先同步真实尺寸，避免 WebGL 画布被初始化为 0x0。
  const width = container.value.clientWidth
  const height = container.value.clientHeight
  if (!width || !height) {
    requestAnimationFrame(refreshGraph)
    return
  }
  // Three.js 提供真实三维坐标、轨道旋转和景深；粒子沿关系线流动强化科幻反馈。
  // 该库先返回图谱实例工厂，再传入挂载容器；直接把容器传给工厂只会得到未挂载的配置对象。
  graphInstance ??= new ForceGraph3D(container.value, { controlType: 'orbit' })
    .backgroundColor(theme.background)
    .showNavInfo(false)
    .enableNodeDrag(true)
    .enableNavigationControls(true)
    // 第二个参数必须是相机注视的三维坐标；传入数字会导致内部读取坐标时中断渲染。
    .cameraPosition({ x: 0, y: 0, z: 720 }, { x: 0, y: 0, z: 0 }, 0)
    .backgroundColor(theme.background)
    /*
      // 每帧确保雾化参数与主题一致，拉开远近节点的空间层次。
      scene.fog = new THREE.Fog(theme.fog, 260, 1200)
    */
    // 官方 hover 提示专门承载上下文信息；常驻 3D 标签只显示名称，避免两者重复。
    .nodeLabel(
      (node) =>
        `<div class="graph-tooltip"><strong>实体</strong><b>${escapeHtml(node.label)}</b><span>${escapeHtml(node.visualLabel)} · ${node.degree} 条关系</span></div>`
    )
    .nodeThreeObject(makeNodeObject)
    .nodeThreeObjectExtend(false)
    .linkLabel(
      (link) =>
        `<div class="graph-tooltip graph-tooltip-link"><strong>关系</strong><b>${escapeHtml(link.label || link.visualLabel)}</b><span>${escapeHtml(link.visualLabel)}</span></div>`
    )
    .linkColor(() => {
        // 白色画布上使用统一深色关系线，节点颜色才承担实体类型区分。
      return RELATION_COLOR
    })
    .linkMaterial(() =>
      // 使用不受灯光影响的基础材质，避免远处连线因光照和雾化变成不可见。
      new THREE.MeshBasicMaterial({
        ...props.edgeStyleOptions.material,
        // 关系颜色按产品约定统一为灰白色，避免外部旧配置再次覆盖统一视觉。
        color: RELATION_COLOR,
        // 关系线保持完全不透明；白底上的半透明细线会因抗锯齿看起来像断线。
        transparent: false,
        opacity: 1,
        depthWrite: false,
        // 黑色线条不使用加法混合，避免在白底上被混合成发灰的低对比度线。
        blending: THREE.NormalBlending
      })
    )
    .linkWidth((link) => {
      const activeNodeId = focusedNodeId || hoveredNodeId
      const source = String(link.source?.id ?? link.source)
      const target = String(link.target?.id ?? link.target)
      return activeNodeId &&
        relatedNodeIds(activeNodeId).has(source) &&
        relatedNodeIds(activeNodeId).has(target)
        // 线宽按“聚焦 > 核心 > 普通”递减；保留最小可见像素，避免白底上消失。
        ? 0.14
        : link.isCore ? 0.08 : 0.035
    })
    .linkOpacity((link) => {
      const activeNodeId = focusedNodeId || hoveredNodeId
      // 不再降低普通线透明度，避免细线在远处或节点边缘出现断续错觉。
      if (!activeNodeId) return 1
      const source = String(link.source?.id ?? link.source)
      const target = String(link.target?.id ?? link.target)
      return relatedNodeIds(activeNodeId).has(source) && relatedNodeIds(activeNodeId).has(target)
        ? 1
        : 0.35
    })
    .linkCurvature(0)
    .linkDirectionalArrowLength((link) => (link.isCore ? 3 : 2))
    .linkDirectionalArrowRelPos(1)
    // 关系线、箭头和流动粒子统一使用白色，颜色只由节点类型承担，降低阅读干扰。
    .linkDirectionalArrowColor(() => RELATION_COLOR)
    .linkThreeObject(makeLinkObject)
    .linkThreeObjectExtend(true)
    .linkPositionUpdate((sprite, { start, end }) => {
      // 官方在 linkThreeObject 返回空值时仍可能触发此回调；空对象必须直接跳过，不能中断渲染循环。
      if (!sprite?.position || !start || !end) return
      // 标签固定在关系线中点，且由 Sprite 始终面向相机，旋转视角时依旧可读。
      sprite.position.x = start.x + (end.x - start.x) * 0.5
      sprite.position.y = start.y + (end.y - start.y) * 0.5
      sprite.position.z = start.z + (end.z - start.z) * 0.5
    })
    .linkDirectionalParticles((link) => {
      const activeNodeId = focusedNodeId || hoveredNodeId
      return activeNodeId &&
        relatedNodeIds(activeNodeId).has(String(link.source?.id ?? link.source))
        ? 5
        : link.isCore ? 1 : 0
    })
    // 降低粒子移动速度，避免短关系线上的粒子一闪而过。
    .linkDirectionalParticleSpeed(0.0022)
    .linkDirectionalParticleWidth(0.24)
    .linkDirectionalParticleColor(() => '#1682b8')
    .linkDirectionalParticleThreeObject(makeParticleObject)
    .cooldownTicks(120)
    .warmupTicks(30)
    // 使用官方相机 API 固定初始视角；不依赖未稳定的 3D 包围盒，避免 zoomToFit 把镜头拉到空白处。
    .onEngineStop(() => {
      if (!props.autoFit || !isMounted || autoFitDone || userInteracted) return
      // 过滤尚未完成力布局的 NaN/无穷坐标，避免无效布局触发相机更新。
      const validNodes = currentNodes.filter((node) =>
        [node.x, node.y, node.z].every((value) => Number.isFinite(value))
      )
      if (!validNodes.length) return
      autoFitDone = true
      // 初始节点坐标半径受控，固定距离能保证首屏可见且不会在一秒后再次缩小。
      graphInstance.cameraPosition({ x: 0, y: 0, z: 760 }, { x: 0, y: 0, z: 0 }, 0)
      console.info('[知识图谱3D] 初始取景完成', { nodes: validNodes.length })
    })
    .graphData(data)
  if (isNewInstance) {
    // OrbitControls 的 start 事件表示用户开始缩放、旋转或平移，此后自动取景不得覆盖用户视角。
    graphInstance.controls().addEventListener('start', () => {
      userInteracted = true
    })
  }
  if (!isNewInstance) {
    // 复用实例时显式替换数据，否则响应式刷新只会更新统计面板而不会更新 Three.js 场景。
    graphInstance.graphData(data)
  }
  // 记录渲染前的真实容器与数据状态，便于定位页面被隐藏或图数据未挂载的情况。
  console.info('[知识图谱3D] 准备渲染', {
    width,
    height,
    nodes: data.nodes.length,
    links: data.links.length,
    newInstance: isNewInstance
  })
  graphInstance.backgroundColor(theme.background)
  // 让连接更近、排斥更克制，形成以关联簇为中心的结构，而不是散开的发光球集合。
  graphInstance.d3Force('link')
    ?.distance(props.layoutOptions.linkDistance ?? 56)
    .strength(props.layoutOptions.linkStrength ?? 0.9)
  graphInstance.d3Force('charge')?.strength(props.layoutOptions.chargeStrength ?? -55)
  // scene() 是当前版本公开的场景访问入口；仅在实例创建完成后设置雾化。
  graphInstance.scene().fog = new THREE.Fog(theme.fog, 330, 1350)
  // 两组异色光源强化前后层次，避免节点在三维旋转时看起来仍像二维圆点。
  // 环境光压低、主光源增强，保证球面一侧有高光、另一侧有自然暗部。
  graphInstance.lights([
    new THREE.AmbientLight('#8aa4b8', 0.5),
    new THREE.DirectionalLight('#d9f4ff', 2.1),
    new THREE.PointLight('#35d6ff', 1.8, 900),
    new THREE.PointLight('#9b7cff', 1.1, 700)
  ])
  graphInstance.width(width).height(height)
  console.info('[知识图谱3D] 画布已同步尺寸', {
    width: graphInstance.width(),
    height: graphInstance.height()
  })
  if (isNewInstance) graphInstance
    .onNodeClick((node) => {
      focusedNodeId = String(node.id)
      refreshGraphScene()
      emit('node-click', toNodePayload(node))
    })
    .onNodeHover((node) => {
      hoveredNodeId = node ? String(node.id) : null
      refreshGraphScene()
    })
    .onLinkClick((link) => emit('edge-click', toEdgePayload(link)))
    .onBackgroundClick(() => {
      clearFocus()
      emit('canvas-click')
    })
  if (isNewInstance) emit('ready', graphInstance)
  emit('data-rendered', data)
}
function toNodePayload(node) {
  return {
    id: node.id,
    data: {
      label: node.label,
      visualLabel: node.visualLabel,
      color: node.color,
      degree: node.degree,
      impact: node.impact,
      original: node.original
    }
  }
}
function toEdgePayload(link) {
  return {
    id: link.id,
    source: link.source?.id ?? link.source,
    target: link.target?.id ?? link.target,
    data: {
      label: link.label,
      visualLabel: link.visualLabel,
      color: link.color,
      original: link.original
    }
  }
}
function refreshGraph() {
  clearTimeout(renderTimer)
  // 等待 graphData 响应式更新和 v-show 完成后再初始化/刷新 3D 引擎。
  renderTimer = setTimeout(() => {
    nextTick(renderGraph)
  }, 0)
}
function fitView() {
  graphInstance?.zoomToFit(600, 80)
}
function fitCenter() {
  fitView()
}
function getInstance() {
  return graphInstance
}
function setGraphData(data) {
  // 兼容旧组件暴露的 setData：外部传入完整图数据时直接交给 3D 引擎，否则刷新当前响应式数据。
  if (data && graphInstance) {
    const links = (data.links || data.edges || []).map((edge, index) => ({
      ...edge,
      id: edge.id ? String(edge.id) : `edge-${index}`,
      source: String(edge.source ?? edge.source_id),
      target: String(edge.target ?? edge.target_id)
    }))
    const normalized = {
      nodes: data.nodes || [],
      links
    }
    currentNodes = normalized.nodes
    currentEdges = normalized.links
    graphInstance.graphData(normalized)
    refreshGraphScene()
  }
  else refreshGraph()
}
function focusNode(id) {
  if (
    !graphInstance ||
    !props.enableFocusNeighbor ||
    !currentNodes.some((node) => node.id === String(id))
  )
    return
  focusedNodeId = String(id)
  refreshGraphScene()
}
function clearFocus() {
  focusedNodeId = null
  hoveredNodeId = null
  refreshGraphScene()
}
function applyHighlightKeywords() {
  const keywords = (props.highlightKeywords || [])
    .map((keyword) => String(keyword).trim().toLowerCase())
    .filter(Boolean)
  highlightedNodeIds = new Set(
    currentNodes
      .filter((node) =>
        keywords.some((keyword) => String(node.label).toLowerCase().includes(keyword))
      )
      .map((node) => node.id)
  )
  graphInstance?.refresh()
}
function clearHighlights() {
  highlightedNodeIds = new Set()
  refreshGraphScene()
}
watch(() => props.graphData, refreshGraph, { deep: true })
watch(() => props.highlightKeywords, applyHighlightKeywords, { deep: true })
watch(() => themeStore.isDark, refreshGraph)
onMounted(() => {
  isMounted = true
  if (props.autoResize) {
    resizeObserver = new ResizeObserver(() => {
      if (container.value && graphInstance) {
        graphInstance.width(container.value.clientWidth)
        graphInstance.height(container.value.clientHeight)
      }
    })
    if (container.value) resizeObserver.observe(container.value)
  }
  renderGraph()
})
onUnmounted(() => {
  isMounted = false
  clearTimeout(renderTimer)
  resizeObserver?.disconnect()
  disposeTrackedThreeObjects()
  graphInstance?._destructor()
  graphInstance = null
})
defineExpose({
  refreshGraph,
  fitView,
  fitCenter,
  getInstance,
  focusNode,
  clearFocus,
  setData: setGraphData,
  applyHighlightKeywords,
  clearHighlights
})
</script>

<style lang="less">
.graph-canvas-container {
  position: relative;
  width: 100%;
  height: 100%;
  overflow: hidden;
  background-color: #ffffff;
  background-image:
    linear-gradient(rgba(82, 97, 116, 0.08) 1px, transparent 1px),
    linear-gradient(90deg, rgba(82, 97, 116, 0.08) 1px, transparent 1px);
  background-size: 32px 32px;
}
.graph-canvas-container.is-dark {
  background-color: #ffffff;
  background-image:
    linear-gradient(rgba(82, 97, 116, 0.08) 1px, transparent 1px),
    linear-gradient(90deg, rgba(82, 97, 116, 0.08) 1px, transparent 1px);
}
.graph-canvas {
  width: 100%;
  height: 100%;
}
.graph-tooltip {
  display: flex;
  min-width: 150px;
  flex-direction: column;
  gap: 3px;
  padding: 8px 10px;
  color: #172235;
  background: rgba(255, 255, 255, 0.97);
  border: 1px solid #7b8798;
  border-left: 3px solid #111827;
  border-radius: 4px;
  box-shadow: 0 5px 16px rgba(15, 23, 42, 0.2);
  font-size: 12px;
  line-height: 1.35;
  pointer-events: none;
}
.graph-tooltip strong {
  color: #4b5563;
  font-size: 10px;
  letter-spacing: 0.08em;
}
.graph-tooltip b {
  color: #111827;
  font-size: 14px;
}
.graph-tooltip span {
  color: #526174;
}
.graph-tooltip-link {
  border-left-color: #334155;
}
.graph-relationship-legend {
  position: absolute;
  right: 14px;
  bottom: 14px;
  display: flex;
  max-width: min(420px, calc(100% - 32px));
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  padding: 8px 10px;
  color: #d7eefa;
  background: rgba(5, 15, 32, 0.78);
  border: 1px solid rgba(102, 213, 255, 0.32);
  border-radius: 6px;
  box-shadow: 0 0 18px rgba(34, 185, 255, 0.12);
  font-size: 12px;
  line-height: 1.2;
  pointer-events: none;
}
.legend-title {
  color: #7ee5ff;
  font-weight: 600;
}
.legend-item {
  display: inline-flex;
  min-width: 0;
  align-items: center;
  gap: 5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.legend-item i {
  width: 16px;
  height: 3px;
  flex: 0 0 auto;
  box-shadow: 0 0 6px currentColor;
}
.graph-stats-wrapper {
  position: absolute;
  bottom: 10px;
  left: 10px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
  pointer-events: auto;
  z-index: 10;
}
.floating-panel {
  background: var(--color-trans-light);
  backdrop-filter: blur(12px);
  border: 1px solid var(--gray-100);
  border-radius: 8px;
  box-shadow: 0 0 4px 0 var(--shadow-2);
  font-size: 13px;
}
.panel-header {
  padding: 10px 14px;
  border-bottom: 1px solid var(--gray-200);
}
.panel-title {
  color: var(--gray-1000);
  font-weight: 600;
}
.panel-body {
  padding: 10px 14px;
}
.graph-stats-panel {
  display: flex;
  gap: 16px;
  padding: 6px 12px;
}
.stat-item {
  display: flex;
  gap: 4px;
  padding: 2px 0;
  border: 0;
  background: transparent;
  cursor: pointer;
  font: inherit;
}
.stat-item:hover .stat-label,
.stat-item.active .stat-label,
.stat-item:hover .stat-value,
.stat-item.active .stat-value {
  color: var(--main-color);
}
.stat-label {
  color: var(--color-text-secondary);
}
.stat-value {
  color: var(--color-text);
  font-weight: 600;
}
.type-stats-card {
  width: 220px;
  max-width: calc(100vw - 40px);
}
.type-stats-list {
  max-height: 220px;
  overflow-y: auto;
}
.type-stats-row {
  display: grid;
  grid-template-columns: 12px minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
  min-height: 28px;
}
.type-color {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}
.type-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.slots {
  pointer-events: none;
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  z-index: 999;
}
.overlay {
  width: 100%;
  flex: 0 0 auto;
  pointer-events: auto;
}
.canvas-content,
.canvas-content * {
  pointer-events: none;
  flex: 1;
  background: transparent !important;
}
</style>
