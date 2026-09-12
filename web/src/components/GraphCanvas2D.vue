<template>
  <div ref="rootEl" class="graph-canvas-2d">
    <div v-show="graphData.nodes.length > 0" ref="container" class="graph-canvas"></div>
    <div class="slots">
      <div v-if="$slots.top" class="overlay top"><slot name="top" /></div>
      <div class="canvas-content"><slot name="content" /></div>
      <div v-if="graphData.nodes.length > 0" class="graph-stats-wrapper">
        <div
          v-if="activeStatsPanel"
          class="floating-panel type-stats-card"
          :class="`is-${activeStatsPanel}`"
        >
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
                <span class="type-color" :style="item.color ? { backgroundColor: item.color } : undefined"></span
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
      <aside
        v-if="relationshipLegend.length"
        class="graph-relationship-legend"
        aria-label="图谱图例"
      >
        <span class="legend-title">节点</span>
        <span v-for="item in nodeLegend" :key="`node-${item.name}`" class="legend-item">
          <i class="legend-node" :style="{ backgroundColor: item.color }"></i>{{ item.name }}
        </span>
        <span class="legend-title">关系</span>
        <span v-for="item in relationshipLegend" :key="item.name" class="legend-item">
          <i class="legend-edge"></i>{{ item.name }}
        </span>
      </aside>
    </div>
  </div>
</template>

<script setup>
// 知识图谱 2D 扁平渲染实现（sigma.js 3 + graphology 2D WebGL）。
// 与 GraphCanvas.vue（3d-force-graph + Three.js 3D 版）互为备份实现，
// 由 KnowledgeGraphSection.vue 的 USE_FLAT_2D_GRAPH 常量二选一挂载。
// 对外契约（props / emits / slots / defineExpose）与 3D 版保持一致，上层无需改动。
//
// 与 3D 版的已知差异（均为渲染器能力边界，非缺陷）：
// 1. sigma 是纯 2D 渲染器，NodeDisplayData 只有 x/y，没有 z，因此没有景深、透视与视角旋转；
// 2. 没有沿关系线流动的粒子动画；
// 3. 没有 Three.js 球体材质、光晕、雾化、多光源（因此 3D 版的 nodeStyleOptions /
//    edgeStyleOptions 两个 Three.js 材质配置项在本实现中不适用，未做保留）；
// 4. 节点与关系标签改用 sigma 内置的 canvas 绘制，替代 3D 版的 Sprite 纹理与 HTML 浮层。
import Graph from 'graphology'
import Sigma from 'sigma'
import { forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY } from 'd3'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { GLOW_RING_NODE_TYPE, GLOW_SCALE, GlowRingNodeProgram } from './graphGlowNodeProgram'

const props = defineProps({
  graphData: { type: Object, required: true, default: () => ({ nodes: [], edges: [] }) },
  labelField: { type: String, default: 'name' },
  autoFit: { type: Boolean, default: true },
  autoResize: { type: Boolean, default: true },
  layoutOptions: { type: Object, default: () => ({}) },
  enableFocusNeighbor: { type: Boolean, default: true },
  sizeByDegree: { type: Boolean, default: true },
  highlightKeywords: { type: Array, default: () => [] }
})
const emit = defineEmits(['ready', 'data-rendered', 'node-click', 'edge-click', 'canvas-click'])

const container = ref(null)
const rootEl = ref(null)
const activeStatsPanel = ref('')

// 非响应式的渲染期状态：这些变量只服务于渲染循环，放进 ref 会带来无意义的依赖追踪开销。
let sigmaInstance = null
let graphInstance = null
let d3Simulation = null
let resizeObserver = null
let renderTimer = null
let isMounted = false
let autoFitDone = false
// 用户是否手动调整过视角（滚轮缩放 / 按下拖拽）。一旦为 true，就不再跟随容器尺寸自动适配，
// 否则每次侧边栏折叠都会把用户辛辛苦苦拖到的位置"拉"回标准视图。
let userAdjustedCamera = false
let focusedNodeId = null
let hoveredNodeId = null
let highlightedNodeIds = new Set()
let currentNodes = []
// 用节点/边的主键反查构建期算好的展示数据（类型色、是否核心、两端 id 等）。
// sigma 的 reducer 只拿到主键和 graphology 属性，需要这张表才能还原业务语义。
const nodeIndex = new Map()
const edgeIndex = new Map()
// 邻接索引：nodeReducer 在聚焦/悬停态下需要为"每个节点"判断是否与聚焦源一跳相连。
// 若每次都从头遍历全量边，复杂度是 O(节点数 × 边数)（例：800 节点 × 2400 边 = 192 万次/帧），
// 而悬停引发的 scheduleRefresh 会按帧触发，必然掉帧。因此预建邻接表把查询降到 O(1) 查表。
const adjacencyIndex = new Map()
// 一跳邻居集合的缓存。同一帧内 reducer 会对所有节点重复查询同一个聚焦源，
// 命中缓存即可直接复用（仅缓存 1 项，因为同一批次内聚焦源恒定）。
let relatedCacheKey = null
let relatedCacheValue = null

const CHUNK_NODE_LABEL = 'Chunk'
const CHUNK_MENTION_EDGE_LABEL = 'MENTIONS'

/* ------------------------------------------------------------------ *
 * 配色（浅色全息 · HUD 风格）
 *
 * 与上一版「Obsidian 白底」的差别不只是换色值，而是三个渲染维度同时改造：
 *   1. 底色：纯白。不加网格/同心环等装饰图层，让发光节点成为唯一焦点；
 *   2. 节点：实心色点 → 核心 + 光环 + 光晕三层（见 graphGlowNodeProgram.js），
 *      光晕负责"发光"，光环负责 HUD 的目标标记感；
 *   3. 色系：7 类实体收敛到冷色系，剔除橙黄——暖色在浅底上会把观感
 *      从"科幻"立刻拉回"统计图表"。
 * 节点按实体类型着色仍是前提：单色渲染时右侧图例的色块与画布对不上，
 * 图例就退化成装饰。底色只写在 <style> 的 .graph-canvas-2d 上——sigma 的
 * WebGL 画布不清屏颜色，靠透明叠加露出容器背景，JS 侧不需要副本。
 * ------------------------------------------------------------------ */
// 关系线：淡天蓝（blue-400）。浅底上的科幻感来自"冷色 + 低存在感"，
// 线只做走向暗示，视觉重心交给带光晕的节点。
const EDGE_COLOR = '#60a5fa'
const EDGE_OPACITY = 0.55
// 聚焦态连线：保持靛蓝（indigo-600），与全景淡蓝拉开层次；
// 刻意避开红色系——红在医学语境里天然被读成"告警/异常"，且会与节点色相撞。
const HIGHLIGHT_EDGE_COLOR = '#4f46e5'
const HIGHLIGHT_EDGE_OPACITY = 0.9
// 核心圆的不透明度。走发光节点程序后，通透感已由光环/光晕承担，
// 核心保持接近不透明，节点才会"实"。
const NODE_OPACITY = 0.95
// 渲染尺寸倍率 = 光晕外沿倍率（graphGlowNodeProgram.js 的 GLOW_SCALE）。
// 自定义节点着色器把 v_radius 的 1/GLOW_SCALE 留给核心圆，其余全部让给光晕，
// 所以节点要按同一倍率放大，核心才能维持改动前的视觉大小。
// 布局仍用原始 size，不受这个纯渲染系数影响。这两个数字是一组，不能单独改。
const NODE_RENDER_SIZE_FACTOR = GLOW_SCALE
// 聚焦态下非相关节点的虚化程度。
const FADED_NODE_OPACITY = 0.1
// 标签兜底色。节点各自带 labelColor 属性（= 自身类型色），
// 这个值只在属性缺失时兜底，保证任何情况下标签都可读。
const NODE_LABEL_COLOR = '#334155'
// sigma 的颜色混合是 gl.blendFunc(ONE, ONE_MINUS_SRC_ALPHA)——预乘混合，
// 而它的顶点着色器把颜色原样直通（v_color = a_color），不做预乘。
// 因此 alpha 小于 1 的颜色必须由调用方预乘，否则会以满亮度盖上去（实测 #2efff1@0.3
// 会渲染成 #42ffff 的刺眼青色，而不是预期的 #21605c）。
// 验证记录见 .workbuddy/memory/2026-09-11.md。
const EDGE_STROKE = premultipliedColor(EDGE_COLOR, EDGE_OPACITY)
const HIGHLIGHT_EDGE_STROKE = premultipliedColor(HIGHLIGHT_EDGE_COLOR, HIGHLIGHT_EDGE_OPACITY)
// 聚焦态虚化色固定用中性深灰：这类节点的语义是"退到背景的位置参照"，
// 再按类型着色会让虚化层比主图还花。
const FADED_NODE_FILL = withAlphaHex('#334155', FADED_NODE_OPACITY)
// 实体类型 → 节点色。浅底上的取色标准改为"冷色系 + 色相拉开 + 中深明度"，
// 让 1.5~5px 的核心圆也能被一眼分辨。暖色（橙/黄）整体退场：它们会破坏
// 冷色调的科幻气质。命中关键词走语义色，未命中的按名称哈希取回退色，
// 保证同一类型在任何一次渲染里颜色恒定。
const TYPE_COLOR_RULES = [
  { keywords: ['药物', '药品', '药材', '方剂', '中成药'], color: '#7c3aed' },
  { keywords: ['疾病', '病症', '诊断'], color: '#0ea5e9' },
  { keywords: ['症状', '证候', '证型', '体征'], color: '#0891b2' },
  { keywords: ['穴位', '腧穴', '经络'], color: '#4f46e5' },
  { keywords: ['人体部位', '部位', '器官', '解剖'], color: '#0d9488' },
  { keywords: ['科室', '部门', '医院', '机构'], color: '#64748b' },
  { keywords: ['检查', '检验', '指标'], color: '#334155' }
]
// 回退池与语义色同色系，保证未分类类型落进来也不会跳出冷色调。
const TYPE_COLOR_FALLBACK = [
  '#7c3aed', '#0ea5e9', '#0891b2', '#4f46e5',
  '#0d9488', '#64748b', '#334155', '#0369a1'
]
// 手动力导向推进的迭代次数。sigma 自身不提供布局，必须外部算好坐标再喂进去。
// 加入引力后收敛路径变长，220 会停在"还没收拢"的中间态，300 才稳定收敛到紧凑簇团。
// 上限用于兜底超大图谱的首屏耗时：节点过多时减少迭代，优先保证页面不长时间卡死。
const LAYOUT_TICKS_DEFAULT = 300
const LAYOUT_TICKS_MAX_NODES = 1200
const LAYOUT_TICKS_LARGE = 200

function getNodeVisualLabel(node) {
  return node?.type || node?.normalized?.type || node?.properties?.label || 'Entity'
}
function getEdgeVisualLabel(edge) {
  return edge?.type || edge?.normalized?.type || 'RELATED_TO'
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

/* ------------------------------------------------------------------ *
 * 颜色工具
 * 上一版曾把节点压成单色，本轮恢复"按实体类型着色"——类型色是 Obsidian
 * 图谱的信息载体，也是右侧图例能成立的前提（单色时图例色块与画布对不上）。
 * 这里保留三件事：解析 hex、按 alpha 生成预乘色值、按类型名取色。
 * ------------------------------------------------------------------ */
function hexToRgb(hex) {
  const raw = String(hex).replace('#', '')
  const full =
    raw.length === 3
      ? raw
          .split('')
          .map((char) => char + char)
          .join('')
      : raw
  const value = Number.parseInt(full, 16)
  // 非法色值时退回黑色，避免把 NaN 传进 graphology 导致整张图渲染异常。
  if (!Number.isFinite(value)) return { r: 0, g: 0, b: 0 }
  return {
    r: ((value >> 16) & 255) / 255,
    g: ((value >> 8) & 255) / 255,
    b: (value & 255) / 255
  }
}
// 生成 8 位 hex（#rrggbbaa），其中 rgb 分量已乘以 alpha——即预乘色。
// alpha 单独写在第 9、10 位，由 sigma 的 parseColor 读取：
//   parseColor: val.length === 9 → a = parseInt(val.slice(7, 9), 16) / 255
// 为什么必须预乘：见文件顶部 premultipliedColor 调用处的说明。
function premultipliedColor(hex, alpha) {
  const { r, g, b } = hexToRgb(hex)
  const clamped = Math.min(1, Math.max(0, alpha))
  const channel = (value) =>
    Math.round(Math.min(1, Math.max(0, value * clamped)) * 255)
      .toString(16)
      .padStart(2, '0')
  const alphaByte = Math.round(clamped * 255)
    .toString(16)
    .padStart(2, '0')
  return `#${channel(r)}${channel(g)}${channel(b)}${alphaByte}`
}
// 生成 #RRGGBBAA 形式的**未预乘**色值，供自定义节点着色器使用。
// 两者的区别必须分清：sigma 默认的节点/边程序把颜色原样直通，需要调用方预乘；
// 而 graphGlowNodeProgram 在片元里自己乘好了 alpha（为了做连续衰减的光晕），
// 再喂预乘色就会双重预乘、整张图发暗。所以节点色走这里，边色继续走预乘。
function withAlphaHex(hex, alpha) {
  const byte = Math.round(Math.min(1, Math.max(0, alpha)) * 255)
    .toString(16)
    .padStart(2, '0')
  return `${hex}${byte}`
}
// 类型名 → 回退色下标用的稳定哈希。必须是纯函数：画布与图例各自独立调用它，
// 只有结果恒定，两处的颜色才会一致。
function hashString(value) {
  let hash = 0
  for (let index = 0; index < value.length; index += 1)
    hash = (hash << 5) - hash + value.charCodeAt(index)
  return Math.abs(hash | 0)
}
// 按实体类型取节点色：语义关键词优先，未命中的按名称哈希取回退色。
function getTypeColor(label) {
  const name = String(label ?? '')
  for (const rule of TYPE_COLOR_RULES) {
    if (rule.keywords.some((keyword) => name.includes(keyword))) return rule.color
  }
  return TYPE_COLOR_FALLBACK[hashString(name) % TYPE_COLOR_FALLBACK.length]
}
// 节点半径（屏幕像素）。sigma 默认 itemSizesReference 为 screen，尺寸不随缩放变化。
// 点径不是越小越好——fitView 按包围盒缩放间距，而点径锚定屏幕像素，
// 所以把点改小只会让叶子"看不见"、整图显空，并不会变密（实测 2.2 档比 3.2 档更空）。
// 叶子维持 3.2；只把枢纽上限从 14 收到 11：枢纽过大会撑开碰撞半径，
// 整团被推开、关系线反而变长。
function getNodeSize(degree, impact, foreground) {
  if (!foreground) return 3.2
  if (!props.sizeByDegree) return 4.5
  return Math.min(11, 3.4 + Math.sqrt(degree + 1) * 0.95 + Math.min(impact, 12) * 0.06)
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
    // 只有节点图例需要色块；关系线在全景态是单一颜色，交给 CSS 着色。
    color: getColor ? getColor(name) : undefined
  })).sort(
    (a, b) => b.count - a.count || a.name.localeCompare(b.name)
  )
}
const visibleEntityNodes = computed(() => (props.graphData?.nodes || []).filter(isEntityNode))
const visibleRelationshipEdges = computed(() =>
  (props.graphData?.edges || []).filter(isEntityRelationEdge)
)
const visibleEntityCount = computed(() => visibleEntityNodes.value.length)
const visibleRelationshipCount = computed(() => visibleRelationshipEdges.value.length)
const nodeTypeStats = computed(() =>
  buildTypeStats(visibleEntityNodes.value, getNodeVisualLabel, getTypeColor)
)
const edgeTypeStats = computed(() =>
  buildTypeStats(visibleRelationshipEdges.value, getEdgeVisualLabel)
)
const activeTypeStats = computed(() =>
  activeStatsPanel.value === 'node' ? nodeTypeStats.value : edgeTypeStats.value
)
// 图例只展示高频类型，避免把一长串低频类型再次变成画面噪音。
// 节点色块复用 getTypeColor，与画布上的渲染色来自同一个纯函数，不会漂移；
// 关系线在全景态是单一颜色，色块由 CSS 统一着色（见样式块）。
const relationshipLegend = computed(() => edgeTypeStats.value.slice(0, 6))
const nodeLegend = computed(() => nodeTypeStats.value.slice(0, 4))
function toggleStatsPanel(type) {
  activeStatsPanel.value = activeStatsPanel.value === type ? '' : type
}

// 默认挑出连接最多的少量实体作为阅读入口与标签展示范围；
// 点击任意节点后仍可查看完整一跳关系。阈值与 3D 版保持一致，避免两套实现的标签密度观感差异过大。
function getCoreNodeIds(nodes) {
  const coreCount = Math.min(18, Math.max(8, Math.ceil(nodes.length * 0.2)))
  return new Set(
    [...nodes]
      .sort((left, right) => right.degree - left.degree || right.impact - left.impact)
      .slice(0, coreCount)
      .map((node) => node.id)
  )
}

function buildGraphData() {
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
    const impact = normalizeImpact(node.citations ?? node.references, degree)
    // 用黄金角螺旋生成初始坐标，让力导向从一个均匀铺开的圆盘开始收敛，
    // 避免所有节点从同一点起步产生数值退化（坐标全等会让斥力方向失效）。
    const angle = index * 2.399963229728653
    const radius = 44 + Math.sqrt(index + 1) * 18
    const visualLabel = getNodeVisualLabel(node)
    const typeColor = getTypeColor(visualLabel)
    return {
      id,
      label: String(node[props.labelField] ?? node.name ?? id),
      visualLabel,
      // 按实体类型着色。这里刻意传**未预乘**色值：自定义节点着色器为了做连续
      // 衰减的光晕，会在片元里自己乘 alpha 再输出（详见 graphGlowNodeProgram.js）。
      color: withAlphaHex(typeColor, NODE_OPACITY),
      // 未预乘的原色：供点击回调与图例使用，CSS 需要的是正常色值而不是预乘色。
      rawColor: typeColor,
      // 标签跟随节点类型色——Obsidian 的标签就是节点色的版本，
      // 这样"这行字属于哪个点"不必靠空间距离去猜。
      labelColor: typeColor,
      degree,
      impact,
      x: Math.cos(angle) * radius,
      y: Math.sin(angle) * radius,
      original: node
    }
  })
  const nodeIds = new Set(nodes.map((node) => node.id))
  // 边主键：优先沿用后端 id，缺失时按序生成。去重是因为 graphology 不允许重复主键，
  // 而知识图谱中同一对实体之间存在多条不同语义关系的情况很常见。
  const usedEdgeKeys = new Set()
  const links = (raw.edges || [])
    .map((edge, index) => {
      const source = String(edge.source_id)
      const target = String(edge.target_id)
      let key = edge.id ? String(edge.id) : `edge-${index}`
      while (usedEdgeKeys.has(key)) key = `${key}__dup`
      usedEdgeKeys.add(key)
      return {
        key,
        source,
        target,
        label: String(edge.type ?? ''),
        visualLabel: getEdgeVisualLabel(edge),
        color: EDGE_STROKE,
        original: edge
      }
    })
    .filter((edge) => edge.source !== edge.target && nodeIds.has(edge.source) && nodeIds.has(edge.target))
  // 只把度数最高的一小批节点当核心（getCoreNodeIds 内部取前 20%，上限 18 个）。
  // 这里刻意不做"核心边的两端也升为核心"的沿边扩张：在枢纽-叶子拓扑下，
  // 那一步会把每一个叶子都提升为核心（实测 420 节点全部命中），
  // 于是 nodeReducer 里"只给核心节点显示标签"的克制设计被架空，标签会铺满整张画布。
  const coreNodeIds = getCoreNodeIds(nodes)
  for (const node of nodes) {
    node.isCore = coreNodeIds.has(node.id)
    node.size = getNodeSize(node.degree, node.impact, node.isCore)
    // 预计算聚焦态虚化色，避免在 sigma reducer 中做重复的颜色运算。
    // 这里不派生"非核心节点的弱化色"：节点是单一颜色的，
    // 主次关系改由标签显隐、尺寸差与聚焦态承担。
    node.fadedColor = FADED_NODE_FILL
  }
  // 一次性建好无向邻接表（O(边数)），供 reducer 在聚焦/悬停态下做 O(1) 一跳查询。
  adjacencyIndex.clear()
  for (const link of links) {
    if (!adjacencyIndex.has(link.source)) adjacencyIndex.set(link.source, new Set())
    if (!adjacencyIndex.has(link.target)) adjacencyIndex.set(link.target, new Set())
    adjacencyIndex.get(link.source).add(link.target)
    adjacencyIndex.get(link.target).add(link.source)
  }
  // 图数据已重建，上一轮的邻居缓存按主键索引会失效，必须一并作废。
  relatedCacheKey = null
  relatedCacheValue = null
  return { nodes, links }
}

function relatedNodeIds(nodeId) {
  if (!nodeId) return new Set()
  // 命中缓存直接复用：同一帧内 reducer 会以同一个聚焦源查询所有节点。
  if (relatedCacheKey === nodeId && relatedCacheValue) return relatedCacheValue
  const ids = new Set([nodeId])
  for (const neighbor of adjacencyIndex.get(nodeId) || []) ids.add(neighbor)
  relatedCacheKey = nodeId
  relatedCacheValue = ids
  return ids
}
// 当前聚焦源（点击优先于悬停）。为 null 表示处于全景态。
function getActiveNodeId() {
  return focusedNodeId || hoveredNodeId
}

/* ------------------------------------------------------------------ *
 * sigma reducer
 * reducer 返回的是"展示层覆盖值"，不改动 graphology 的真实属性。
 * 这里集中承载 3D 版原有的一套高亮语义：聚焦/悬停 → 一跳邻居高亮，
 * 关键词命中 → 高亮，其余节点在聚焦态下虚化。
 * ------------------------------------------------------------------ */
function nodeReducer(nodeKey, data) {
  const item = nodeIndex.get(nodeKey)
  if (!item) return data
  const activeNodeId = getActiveNodeId()
  const isHighlighted = highlightedNodeIds.has(nodeKey)
  // 全景态：核心节点略大、带标签；非核心节点只有色点，避免文字把扁平画布糊满。
  if (!activeNodeId) {
    return {
      ...data,
      label: item.isCore || isHighlighted ? data.label : null,
      // 渲染尺寸 = 业务尺寸 × 放大系数：发光节点程序把 v_radius 让了一部分给
      // 光环与光晕，不放大核心就比改动前小一圈。
      size: (isHighlighted ? data.size * 1.35 : data.size) * NODE_RENDER_SIZE_FACTOR,
      zIndex: isHighlighted ? 2 : item.isCore ? 1 : 0
    }
  }
  const inFocus = relatedNodeIds(activeNodeId).has(nodeKey)
  if (!inFocus) {
    // 聚焦态下的非相关节点：换成极低不透明度的深点退到背景，并清空标签，
    // 只留位置作为空间参照。光晕由着色器按节点色计算，换色即等于一并熄灭。
    // 尺寸沿用改动前的 2.2 口径（乘以光晕倍率换算成新的 a_size）。
    return {
      ...data,
      color: data.fadedColor ?? FADED_NODE_FILL,
      size: 2.2 * NODE_RENDER_SIZE_FACTOR,
      label: null,
      zIndex: 0
    }
  }
  return {
    ...data,
    size: data.size * 1.35 * NODE_RENDER_SIZE_FACTOR,
    label: data.label,
    zIndex: 2
  }
}
function edgeReducer(edgeKey, data) {
  const item = edgeIndex.get(edgeKey)
  if (!item) return data
  const activeNodeId = getActiveNodeId()
  // 全景态：所有关系统一使用 EDGE_STROKE（中性蓝灰 / 透明度 0.32，预乘）。
  // 此前按"核心 / 非核心"分两档颜色的做法在参考图里并不存在，这里取消分层，
  // 让全图回到参考图那种"均匀、极淡的走向暗示"。
  if (!activeNodeId) {
    return { ...data, color: EDGE_STROKE, size: 1 }
  }
  // 聚焦态：只保留与当前节点直接相连的关系。
  // 2D 扁平图的关系线无法像 3D 那样靠景深自然分层，若不隐藏，几千条灰线会直接压掉聚焦子图的可读性。
  const inFocus = item.source === activeNodeId || item.target === activeNodeId
  if (!inFocus) return { ...data, hidden: true }
  // 聚焦态用高亮连线色（靛蓝 / 透明度 0.9，预乘），与全景态的灰线拉开明确对比。
  return { ...data, color: HIGHLIGHT_EDGE_STROKE, size: 1.8, zIndex: 1 }
}

/* ------------------------------------------------------------------ *
 * 布局
 * sigma 只负责渲染，不提供布局算法。这里用 d3-force 在挂载前同步算好坐标。
 * 选择同步预计算而不是边跑边刷新的原因：sigma 的 refresh() 会重新索引整图，
 * 按帧调用在节点量较大时会明显掉帧；一次性算完再渲染更稳，也不会出现布局抖动。
 * ------------------------------------------------------------------ */
function runForceLayout(nodes, links) {
  // d3-force 会就地改写传入对象（附加 vx/vy/index，并把 link.source/target 换成节点引用），
  // 因此必须用影子副本，布局结果只回收 x/y，避免污染原始图数据与后续的 reducer 查找。
  const shadowNodes = nodes.map((node) => ({ id: node.id, x: node.x, y: node.y, size: node.size }))
  const shadowLinks = links.map((link) => ({ source: link.source, target: link.target }))
  if (!shadowNodes.length) return
  d3Simulation?.stop()
  d3Simulation = forceSimulation(shadowNodes)
    .force(
      'link',
      forceLink(shadowLinks)
        .id((node) => node.id)
        // 边距 56 → 30：原值让每条关系线都要跨越大半张画布，整图读起来是
        // "放射状长线"而不是一团。缩短后关系线只表达就近连接。
        .distance(props.layoutOptions.linkDistance ?? 30)
        .strength(props.layoutOptions.linkStrength ?? 0.9)
    )
    .force(
      'charge',
      // 斥力 -55 → -30：叶子节点只连一个枢纽，斥力越大它被推得越远，
      // 于是所有叶子停在以枢纽为圆心的等距圈上，形成放射状"星爆"。
      // 减弱斥力后叶子回到枢纽附近，整图才收得成一团。
      forceManyBody().strength(props.layoutOptions.chargeStrength ?? -30)
    )
    // 用指向原点的弱引力替代 forceCenter：forceCenter 只平移质心、不压缩结构，
    // 而"抱成团"需要的正是把节点往中心拉。引力还能把弱连接的边缘节点收回来，
    // 否则它们会飘到远处把包围盒撑大，fitView 反而把主体缩得很小（实测现象）。
    .force('gravityX', forceX(0).strength(props.layoutOptions.gravityStrength ?? 0.25))
    .force('gravityY', forceY(0).strength(props.layoutOptions.gravityStrength ?? 0.25))
    // 碰撞力避免节点重叠；半径带一点余量，否则球面刚好相切时标签仍会打架。
    .force(
      'collide',
      forceCollide((node) => node.size + 1.5).iterations(2)
    )
    // 关闭 d3 自带的 rAF 定时器，改为下面手动推进，保证布局在渲染前一次算完。
    .stop()
  const ticks = shadowNodes.length > LAYOUT_TICKS_MAX_NODES ? LAYOUT_TICKS_LARGE : LAYOUT_TICKS_DEFAULT
  const startedAt = Date.now()
  for (let index = 0; index < ticks; index += 1) d3Simulation.tick()
  d3Simulation.stop()
  for (const node of shadowNodes) {
    const target = nodeIndex.get(node.id)
    // 力导向在极端输入下可能产出 NaN/Infinity，写回前必须过滤，
    // 否则 sigma 的坐标归一化会得到无效 bbox，整张图会渲染成空白。
    if (!target || !Number.isFinite(node.x) || !Number.isFinite(node.y)) continue
    target.x = node.x
    target.y = node.y
    // 必须同步进 graphology：sigma 渲染以及 getBBox / getGraphDimensions 读的都是
    // graphology 的节点属性。只改上面的 JS 对象，等于整个力导向布局从未生效
    // （此前边距 / 斥力 / 引力 / 迭代次数四个参数全是死代码，改它们不会有任何画面变化）。
    graphInstance?.mergeNodeAttributes(target.id, { x: node.x, y: node.y })
  }
  console.info('[知识图谱2D] 力导向布局完成', {
    nodes: shadowNodes.length,
    links: shadowLinks.length,
    ticks,
    elapsedMs: Date.now() - startedAt
  })
}

// 把构建好的节点/边写入 graphology 实例。sigma 直接读 graphology 的属性做渲染。
function applyGraphToInstance(data) {
  graphInstance = new Graph({ multi: true, type: 'directed' })
  nodeIndex.clear()
  edgeIndex.clear()
  for (const node of data.nodes) {
    nodeIndex.set(node.id, node)
    graphInstance.addNode(node.id, {
      x: node.x,
      y: node.y,
      label: node.label,
      size: node.size,
      color: node.color,
      // 标签色的读取路径是 labelColor.attribute → data[该属性]，
      // 所以颜色必须挂成节点属性，settings 里只给兜底色。
      labelColor: node.labelColor,
      // 把预计算的虚化色挂在属性上，reducer 才能通过第二参数取到。
      fadedColor: node.fadedColor
    })
  }
  for (const link of data.links) {
    edgeIndex.set(link.key, link)
    graphInstance.addEdgeWithKey(link.key, link.source, link.target, {
      label: link.visualLabel,
      // size 只是建图初值，实际宽度每帧由 edgeReducer 覆盖。
      size: 1,
      color: EDGE_STROKE
    })
  }
}

function renderGraph() {
  if (!isMounted || !container.value) return
  const width = container.value.clientWidth
  const height = container.value.clientHeight
  // 容器在 v-show 隐藏或 flex 未完成布局时尺寸为 0，此时创建 sigma 会抛错或渲染出空白画布。
  // 与 3D 版同样处理：等下一帧尺寸就绪后重试。
  if (!width || !height) {
    requestAnimationFrame(refreshGraph)
    return
  }
  const data = buildGraphData()
  if (!data.nodes.length) {
    // 无数据时销毁实例，避免残留一张空 WebGL 画布吃掉内存。
    if (sigmaInstance) {
      sigmaInstance.kill()
      sigmaInstance = null
    }
    graphInstance = null
    currentNodes = []
    return
  }
  autoFitDone = false
  userAdjustedCamera = false
  currentNodes = data.nodes
  applyHighlightKeywords()
  applyGraphToInstance(data)
  runForceLayout(data.nodes, data.links)

  if (sigmaInstance) {
    // 已有实例：换掉底层图后需要重新注册 reducer 与事件之外的引用，直接重建最稳。
    sigmaInstance.kill()
    sigmaInstance = null
  }
  sigmaInstance = new Sigma(graphInstance, container.value, {
    renderLabels: true,
    // 关系标签默认关闭：2D 扁平图上线标签数量一多会互相压字，关系语义靠悬停/点击查看。
    renderEdgeLabels: false,
    // sigma 的默认边类型键 'line' 实际绑定的是 EdgeRectangleProgram（三角面片拼的矩形/细线），
    // 不是 EdgeLineProgram（gl.LINES 真 1px）。保留默认实现是因为它按设备像素比正确缩放，
    // 而 gl.LINES 的 lineWidth 在高分屏上恒为 1 物理像素，白底细线会糊成发丝。
    defaultEdgeType: 'line',
    // 改用自定义的"核心 + 光环 + 光晕"节点程序（见 graphGlowNodeProgram.js）。
    // sigma 原生不支持逐节点发光，官方把节点着色器开放为扩展点，
    // @sigma/node-border 与这里的发光程序都是基于同一套工厂实现。
    defaultNodeType: GLOW_RING_NODE_TYPE,
    nodeProgramClasses: { [GLOW_RING_NODE_TYPE]: GlowRingNodeProgram },
    // 悬停态也必须注册，否则 hover 时找不到对应程序，节点会闪回默认圆点。
    nodeHoverProgramClasses: { [GLOW_RING_NODE_TYPE]: GlowRingNodeProgram },
    defaultEdgeColor: EDGE_STROKE,
    // 官方 DEFAULT_SETTINGS 中 minEdgeThickness 默认为 1.7，即关系线的渲染宽度有 1.7px 下限，
    // edgeReducer 里返回更小的 size 不会生效（这一点此前被误判为"参数调不动"）。
    // 这里保持官方默认值，改用 32% 的透明度来压低线条存在感。
    // 标签色走节点属性。官方 drawDiscNodeLabel 的读取顺序是
    // labelColor.attribute → data[该属性] → labelColor.color 兜底，
    // 挂属性后标签自动跟随节点类型色，与右侧图例口径一致。
    labelColor: { attribute: 'labelColor', color: NODE_LABEL_COLOR },
    labelSize: 11,
    labelWeight: '500',
    // 阈值设为 0 交由 nodeReducer 精确控制标签显隐，避免 sigma 的尺寸阈值与业务规则叠加后难以预期。
    labelRenderedSizeThreshold: 0,
    // 密集小节点场景下标签很容易互相压字，
    // 因此放宽最小间距（density 调小 = 允许更少标签同屏），优先保证可读性。
    labelDensity: 0.85,
    labelGridCellSize: 96,
    minCameraRatio: 0.05,
    maxCameraRatio: 8,
    // 必须开启边事件，否则 clickEdge 不会触发，点关系线弹详情的能力会静默失效。
    enableEdgeEvents: true,
    // 开启 z 序渲染，聚焦/高亮节点才能稳定压在普通节点与关系线之上。
    zIndex: true,
    stagePadding: 24,
    nodeReducer,
    edgeReducer
  })

  sigmaInstance.on('clickNode', ({ node }) => {
    if (!props.enableFocusNeighbor) {
      emit('node-click', toNodePayload(nodeIndex.get(node)))
      return
    }
    focusedNodeId = String(node)
    sigmaInstance.scheduleRefresh()
    emit('node-click', toNodePayload(nodeIndex.get(node)))
  })
  sigmaInstance.on('clickEdge', ({ edge }) => {
    emit('edge-click', toEdgePayload(edgeIndex.get(edge)))
  })
  sigmaInstance.on('clickStage', () => {
    clearFocus()
    emit('canvas-click')
  })
  sigmaInstance.on('enterNode', ({ node }) => {
    if (hoveredNodeId === node) return
    hoveredNodeId = node
    // scheduleRefresh 会把刷新合并到下一帧，避免连续 hover 触发整图重算。
    sigmaInstance.scheduleRefresh()
  })
  sigmaInstance.on('leaveNode', () => {
    if (hoveredNodeId === null) return
    hoveredNodeId = null
    sigmaInstance.scheduleRefresh()
  })

  if (props.autoFit && !autoFitDone) {
    autoFitDone = true
    fitView({ animate: false })
  }
  console.info('[知识图谱2D] 渲染完成', {
    width,
    height,
    nodes: data.nodes.length,
    edges: data.links.length,
    // 正常应稳定在 { x: 0.5, y: 0.5, ratio: 1.2 }；若数值是几十几百，
    // 说明相机又跑回原始坐标系了（即"初始化空白"复发）。
    camera: sigmaInstance.getCamera().getState()
  })
  emit('ready', sigmaInstance)
  emit('data-rendered', data)
}

function toNodePayload(node) {
  if (!node) return null
  return {
    id: node.id,
    data: {
      label: node.label,
      visualLabel: node.visualLabel,
      // 回调方拿到的是未预乘的类型原色，可直接用于 CSS。
      color: node.rawColor,
      degree: node.degree,
      impact: node.impact,
      original: node.original
    }
  }
}
function toEdgePayload(link) {
  if (!link) return null
  return {
    id: link.key,
    source: link.source,
    target: link.target,
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
  // 等 graphData 的响应式更新与 v-show 的显示切换落定后再重建，避免读到中间态尺寸。
  renderTimer = setTimeout(() => {
    nextTick(renderGraph)
  }, 0)
}

// sigma 3 在 process() 里会用 createNormalizationFunction 把节点坐标归一化进 [0,1]×[0,1]，
// 相机状态因此也活在归一化坐标系里——官方 Camera#animatedReset() 给出的标准视图是
// 与数据无关的常量 { x: 0.5, y: 0.5, ratio: 1, angle: 0 }。
//
// 这里必须用归一化坐标，不能拿 getBBox() 的原始坐标当相机目标：
// 原始包围盒中心量级是几十到几百（布局以原点为中心），把它喂给相机等于把镜头
// 推到图的左上角之外，初始化画面于是整片纯白，只能靠手动滚轮缩小再拖回来
// （此前"初始化空白"的根因）。
//
// ratio 的标定：归一化后图的最大跨度恒为 1，ratio = 1 时正好贴合视口短边
// （宽高比差异由 sigma 内部的 correctionRatio 补偿），乘 1.2 留出边距，
// 避免边缘节点与标签贴边被裁。
const FIT_CAMERA_RATIO = 1.2
const FIT_CAMERA_STATE = { x: 0.5, y: 0.5, ratio: FIT_CAMERA_RATIO, angle: 0 }

// 回到标准视图，等价于 3D 版的 zoomToFit。
function fitView({ animate = true } = {}) {
  if (!sigmaInstance) return
  const camera = sigmaInstance.getCamera()
  // 首次自动适配走 setState 直接到位：此时相机还停在默认视图，
  // 用动画会先让用户看到一段空白过渡，观感上像是"图没加载出来"。
  if (animate) camera.animate({ ...FIT_CAMERA_STATE }, { duration: 320 })
  else camera.setState({ ...FIT_CAMERA_STATE })
}
// 用户一旦手动缩放或拖拽，就停止跟随容器尺寸自动适配。
function markCameraAdjusted() {
  userAdjustedCamera = true
}
function fitCenter() {
  fitView()
}
function getInstance() {
  return sigmaInstance
}
function setGraphData(data) {
  // 兼容旧组件暴露的 setData；本实现的数据源始终是 props.graphData，
  // 传入完整数据时仅保留节点/边数量日志，便于排查调用方误用。
  if (data) {
    console.info('[知识图谱2D] setData 被调用，实际渲染仍以 graphData 属性为准', {
      nodes: (data.nodes || []).length,
      edges: (data.links || data.edges || []).length
    })
  }
  refreshGraph()
}
function focusNode(id) {
  if (!props.enableFocusNeighbor) return
  if (!currentNodes.some((node) => node.id === String(id))) return
  focusedNodeId = String(id)
  sigmaInstance?.scheduleRefresh()
}
function clearFocus() {
  focusedNodeId = null
  hoveredNodeId = null
  sigmaInstance?.scheduleRefresh()
}
function applyHighlightKeywords() {
  const keywords = (props.highlightKeywords || [])
    .map((keyword) => String(keyword).trim().toLowerCase())
    .filter(Boolean)
  highlightedNodeIds = new Set(
    currentNodes
      .filter((node) => keywords.some((keyword) => node.label.toLowerCase().includes(keyword)))
      .map((node) => node.id)
  )
  sigmaInstance?.scheduleRefresh()
}
function clearHighlights() {
  highlightedNodeIds = new Set()
  sigmaInstance?.scheduleRefresh()
}

watch(() => props.graphData, refreshGraph, { deep: true })
watch(() => props.highlightKeywords, applyHighlightKeywords, { deep: true })

onMounted(() => {
  isMounted = true
  if (props.autoResize) {
    // 容器尺寸变化时同步 WebGL 画布。sigma 自带窗口 resize 监听，
    // 但侧边栏折叠等不触发 window.resize 的布局变化需要 ResizeObserver 兜底。
    resizeObserver = new ResizeObserver(() => {
      if (!sigmaInstance || !container.value) return
      // 尺寸为 0（容器被 v-show 隐藏）时跳过：把画布 resize 到 0 会让 sigma 记下无效视口。
      if (!container.value.clientWidth || !container.value.clientHeight) return
      sigmaInstance.resize()
      // 容器尺寸变化会改变视口比例，而相机的归一化状态不会自己跟着变。
      // 只要用户还没手动调过视角就重新适配，避免侧边栏折叠 / 窗口缩放后图谱跑出视野。
      if (props.autoFit && !userAdjustedCamera) fitView({ animate: false })
    })
    if (container.value) resizeObserver.observe(container.value)
  }
  if (container.value) {
    container.value.addEventListener('wheel', markCameraAdjusted, { passive: true })
    container.value.addEventListener('pointerdown', markCameraAdjusted, { passive: true })
  }
  renderGraph()
})

onUnmounted(() => {
  isMounted = false
  clearTimeout(renderTimer)
  if (container.value) {
    container.value.removeEventListener('wheel', markCameraAdjusted)
    container.value.removeEventListener('pointerdown', markCameraAdjusted)
  }
  resizeObserver?.disconnect()
  resizeObserver = null
  // 必须显式 stop()，否则 d3 的内部定时器会继续持有节点引用，造成内存泄漏。
  d3Simulation?.stop()
  d3Simulation = null
  // sigma 的 kill() 会释放 WebGL 上下文与事件监听；不调用会耗尽浏览器的 WebGL 上下文配额。
  sigmaInstance?.kill()
  sigmaInstance = null
  graphInstance = null
  nodeIndex.clear()
  edgeIndex.clear()
  adjacencyIndex.clear()
  relatedCacheKey = null
  relatedCacheValue = null
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

<style scoped lang="less">
/* 使用 scoped：本组件的类名与 GraphCanvas.vue（3D 版）完全相同，
   而 3D 版是全局样式（需要给 Three.js 注入的 .graph-tooltip 生效）。
   scoped 会在选择器上追加 [data-v-xxx]，特异性高于 3D 版的同名全局规则，
   因此两套组件同时被 import 时，本组件的浮层/图例只会套用本文件的样式，不会互相覆盖。 */
.graph-canvas-2d {
  position: relative;
  width: 100%;
  height: 100%;
  overflow: hidden;
  /* 纯白底。曾试过"极浅冷蓝 + 30px 网格 + 雷达同心环"做仪表盘纵深，
     但静态装饰图层会瓜分注意力、也让画面显得"糊"；干净的白底反而让
     带光晕的冷色节点自己成为唯一焦点，科幻感来自节点本身而非背景。
     sigma 的 WebGL 画布不清屏颜色，这层容器背景会直接透出，因此底色只需写在这里。 */
  background-color: #ffffff;
}
/* sigma 的 WebGL 画布不清屏颜色，透明叠加在容器背景之上，因此底色直接透出。 */
.graph-canvas {
  width: 100%;
  height: 100%;
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
  /* 白底上的浅色玻璃拟态图例：靠边框与投影从纯白画布上"浮"起来。
     不铺灰底片——纯白画布上压一块半透明灰会像一块脏区。 */
  color: #64748b;
  background: rgba(255, 255, 255, 0.9);
  border: 1px solid rgba(15, 23, 42, 0.1);
  border-radius: 6px;
  box-shadow: 0 1px 6px rgba(15, 23, 42, 0.08);
  font-size: 12px;
  line-height: 1.2;
  pointer-events: none;
}
.legend-title {
  color: #0f172a;
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
  border-radius: 1px;
}
/* 节点色块按实体类型着色，颜色由模板内联给出（与画布共用 getTypeColor）；
   这里只负责形状。选择器带上 .legend-item 是为了压过 .legend-item i 的同名尺寸声明。 */
.legend-item i.legend-node {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  opacity: 0.92;
}
/* 图例里的关系色块代表"全景态的关系线"，取与边同一色值，
   透明度略高于画布上的 0.5，保证 3px 高的细条在白底上仍看得见。 */
.legend-item i.legend-edge {
  background: #94a3b8;
  opacity: 0.7;
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
  /* 这两个浮层沿用全局 CSS 变量，会拿到与浅色画布不协调的配色，
     这里显式覆盖为白底浅色玻璃拟态，与图例保持一致口径。 */
  background: rgba(255, 255, 255, 0.94);
  backdrop-filter: blur(12px);
  border: 1px solid rgba(15, 23, 42, 0.1);
  border-radius: 8px;
  box-shadow: 0 2px 10px rgba(15, 23, 42, 0.1);
  color: #64748b;
  font-size: 13px;
}
.panel-header {
  padding: 10px 14px;
  border-bottom: 1px solid rgba(15, 23, 42, 0.08);
}
.panel-title {
  color: #0f172a;
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
  /* 覆盖全局变量：浅色面板上需要中深灰才读得清，同时弱于右侧数值。 */
  color: #64748b;
}
.stat-value {
  color: #0f172a;
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
  /* 节点面板的色块颜色由模板内联给出（按实体类型）；这里的灰是关系面板的兜底 ——
     关系线在全景态统一一种颜色。 */
  background: #94a3b8;
  opacity: 0.92;
}
.type-stats-card.is-edge .type-color {
  background: #94a3b8;
  opacity: 0.7;
}
.type-name {
  /* 类型名沿用面板文字色：显式声明而不依赖继承，避免将来面板底色再变时文字失去对比度。 */
  color: #0f172a;
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
.canvas-content {
  flex: 1;
  pointer-events: none;
  background: transparent !important;
}
</style>
