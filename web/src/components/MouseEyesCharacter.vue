<template>
  <!--
    登录页左侧装饰插画：品牌卡通小人（红色礼帽男孩，取自品牌参考图的半身）。
    跟随分两层：
      1) 瞳孔跟随鼠标（原瞳孔抠成贴图后精确指向，风格与角色天然一致）；
      2) 整个身体随鼠标方位平移 + 倾斜，读作「朝鼠标走过去」。
    密码框有内容（输入中或已切明文）时眼珠转向画面左上方（「我没在看你的密码」），
    失焦或清空后恢复跟随。
    按需求只显示半身、不做眨眼动作：眨眼/闭眼在写实 3D 脸上无论用色块还是压扁都显假。
    纯装饰元素，对辅助技术整体隐藏（信息由右侧表单文案承载）。
  -->
  <div
    class="mouse-eyes"
    :class="{ 'is-looking-away': isLookingAway }"
    aria-hidden="true"
  >
    <div ref="stageRef" class="stage">
      <img class="hero-img" src="/brand-kid-hero.png" alt="" />

      <span class="eye is-right" data-core="0.805">
        <img class="pupil" src="/brand-kid-pupil-right.png" alt="" />
      </span>
      <span class="eye is-left" data-core="0.732">
        <img class="pupil" src="/brand-kid-pupil-left.png" alt="" />
      </span>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { calcPupilOffset, isCoarsePointer, prefersReducedMotion } from '@/utils/mouseEyes'

/**
 * 密码框状态驱动的动作：密码框有内容（输入中或已切明文）→ 眼珠转向画面左上方
 * （远离密码框），对应「我没在看你的密码」；失焦或清空后恢复跟随鼠标。
 * 之前试过「切明文时闭眼」，用户反馈动作多余，已移除。
 */
const props = defineProps({
  /** 密码输入框是否已有内容 */
  passwordFilled: { type: Boolean, default: false },
  /** 密码是否已切换为明文显示（antd InputPassword 的 visible） */
  passwordVisible: { type: Boolean, default: false },
  /** 用户是否正聚焦在密码输入框上（输入中才「看别处」，失焦恢复跟随） */
  passwordFocused: { type: Boolean, default: false }
})

// 「看别处」时眼神固定朝画面左上方（约 225°，远离右侧的密码输入框），不再跟随鼠标。
// 角度用的是屏幕坐标系（y 向下）：-2.36 rad ⇒ cos<0、sin<0，即左上。
const LOOK_AWAY_ANGLE = -2.36
// 看别处时瞳孔偏移直接取可偏移半径（1.0 倍）：这是「用力看向一侧」的动作，
// 贴到眼眶边缘才够明显；角色缩小后单眼可移动半径只有约 8px，再乘 0.9 会明显偏弱。
const LOOK_AWAY_DISTANCE_RATIO = 1
// 瞳孔贴图四周留了透明边（裁切与羽化用），瞳孔本体只占贴图宽度的一部分；
// 两只眼的本体占比不同（左 0.732、右 0.805，见 template 的 data-core；
// 按遮罩半径 29.5/32.5 ÷ 贴图半宽 41 算出，改 prepare_stand.py 的 MASK_PAD 后必须同步），
// 计算可偏移半径必须按各自本体算，否则瞳孔还没贴到眼眶就会被判成「已经到边」。
const PUPIL_CORE_DEFAULT = 0.805
// 瞳孔与眼眶之间保留的空隙（px）。贴图边缘自带羽化，留 0.5px 就够；
// 取 2px 时在「角色缩小 + 瞳孔贴图占眼球框 82%」的组合下会把可移动半径吃到只剩 2px，
// 屏幕上完全看不出跟随（踩过）。
// 额外留出安全边距，避免透明留白或浏览器小数像素渲染造成贴边溢出。
const PUPIL_MARGIN = 1.5
// 原图眼白是椭圆形，方形定位框在上下方向会比真实眼白更宽，因此额外收紧纵向活动范围。
const PUPIL_VERTICAL_MARGIN = 4
// 截图中画面左侧的眼白横向更窄，右侧极限时单独收紧，避免眼珠越过眼白边缘。
const LEFT_EYE_HORIZONTAL_SCALE = 0.65
// 身体走动的幅度上限：鼠标从画面中心移到边缘时，身体最多平移 16px、倾斜 4°。
// 幅度刻意保持小：全身像整体倾斜超过 5° 就明显读作「人物歪了」而不是「走过去」。
const WALK_SHIFT = 16
const WALK_TILT = 4

const stageRef = ref(null)
const isLookingAway = computed(
  // 只有真正显示密码时才进入回避状态；关闭显示后立即恢复鼠标跟随。
  () => props.passwordFilled && props.passwordVisible
)

let eyes = []
let frameId = 0
let mouseX = 0
let mouseY = 0

/**
 * 把鼠标状态换算成「瞳孔位移 + 身体走动」并写入 DOM。
 *
 * 直接操作元素 style 而不是走响应式数据：mousemove 频率可达每帧多次，
 * 交给 Vue 的渲染队列会让整个组件反复重渲染，而这里只需要改几个 transform。
 */
function render() {
  const stage = stageRef.value
  if (!stage) return

  // 身体走动：以视口中心为原点做水平归一化（-1 靠左、1 靠右）
  const shift = Math.max(-1, Math.min(1, (mouseX - window.innerWidth / 2) / (window.innerWidth / 2)))
  // 显示密码时人物固定侧向输入框的反方向，鼠标移动也不能改变身体朝向。
  const bodyShift = isLookingAway.value ? -1 : shift
  stage.style.transform = `translateX(${(bodyShift * WALK_SHIFT).toFixed(1)}px) rotate(${(
    bodyShift * WALK_TILT
  ).toFixed(2)}deg)`

  eyes.forEach((eye) => {
    if (isLookingAway.value) {
      // 看别处模式：忽略鼠标位置，固定看向画面左上方。
      // 位移按每只眼各自的可偏移半径算（方向一致、幅度最多差 1px，肉眼无感）；
      // 若改成两眼共用较小值，位移会被拉低三成，动作立刻变得不明显（踩过）。
      const distance = eye.maxX * LOOK_AWAY_DISTANCE_RATIO
      eye.pupil.style.transform = `translate(${(Math.cos(LOOK_AWAY_ANGLE) * distance).toFixed(2)}px, ${(
        Math.sin(LOOK_AWAY_ANGLE) * distance
      ).toFixed(2)}px)`
      return
    }

    // 每次重新读眼球包围盒：身体走动与窗口缩放都会让眼睛位置变化，
    // 缓存跨帧复用会导致瞳孔偏移中心逐渐失准。
    const rect = eye.ball.getBoundingClientRect()
    const offset = calcPupilOffset(mouseX, mouseY, rect, eye.maxX, eye.maxY)
    eye.pupil.style.transform = `translate(${offset.x.toFixed(2)}px, ${offset.y.toFixed(2)}px)`
  })
}

/**
 * 计算瞳孔的可偏移半径。
 *
 * 眼球与瞳孔尺寸都用百分比描述（跟随显示尺寸等比缩放），CSS 里给不出固定 px，
 * 因此按实际渲染出的像素宽高动态推算：
 * 半径 = (眼球框宽 - 瞳孔本体宽) / 2 - 留边，瞳孔本体宽 = 贴图宽 × 该眼本体占比。
 */
function measureEyes() {
  eyes.forEach((eye) => {
    // stage 会随鼠标旋转；offset 尺寸不受 transform 影响，避免旋转后的包围盒把活动范围算大。
    const ballWidth = eye.ball.offsetWidth
    const ballHeight = eye.ball.offsetHeight
    const pupilWidth = eye.pupil.offsetWidth
    const pupilHeight = eye.pupil.offsetHeight
    const coreWidth = pupilWidth * eye.core
    const coreHeight = pupilHeight * eye.core
    // 宽高分别计算，确保鼠标在眼睛正上方或正下方时眼珠仍完整留在眼睛内。
    const baseMaxX = Math.max(0, (ballWidth - coreWidth) / 2 - PUPIL_MARGIN)
    eye.maxX = eye.isLeft ? baseMaxX * LEFT_EYE_HORIZONTAL_SCALE : baseMaxX
    eye.maxY = Math.max(0, (ballHeight - coreHeight) / 2 - PUPIL_MARGIN - PUPIL_VERTICAL_MARGIN)
  })
}

function handleMouseMove(event) {
  mouseX = event.clientX
  mouseY = event.clientY
  // rAF 节流：高频 mousemove（高刷屏可达 200Hz）每帧最多重算一次，
  // 同一帧内不重复读取布局，避免样式与布局反复互锁。
  if (frameId) return
  frameId = requestAnimationFrame(() => {
    frameId = 0
    render()
  })
}

// 容器尺寸变化（窗口缩放、卡片宽度变化）后百分比尺寸对应的像素值会变，需要重新量一次
let resizeObserver = null

onMounted(() => {
  eyes = Array.from(stageRef.value?.querySelectorAll('.eye') ?? []).map((el) => ({
    ball: el,
    pupil: el.querySelector('.pupil'),
    // 瞳孔本体占贴图宽度的比例，由素材决定，写在 data-core 上（改素材时一并更新）
    core: Number.parseFloat(el.dataset.core) || PUPIL_CORE_DEFAULT,
    isLeft: el.classList.contains('is-left'),
    maxX: 0,
    maxY: 0
  }))
  measureEyes()

  // 首帧朝向屏幕中心，避免加载瞬间瞳孔与身体都挤在左上角（clientX/Y 初值为 0）
  mouseX = window.innerWidth / 2
  mouseY = window.innerHeight / 2
  render()

  if (typeof ResizeObserver === 'function') {
    resizeObserver = new ResizeObserver(() => {
      measureEyes()
      render()
    })
    resizeObserver.observe(stageRef.value)
  }

  // 关闭动效偏好的用户与触摸设备不绑监听：不跟随，只保留静态插画
  if (prefersReducedMotion() || isCoarsePointer()) return

  window.addEventListener('mousemove', handleMouseMove, { passive: true })
})

// 「看别处」状态切换后需要立刻重算瞳孔（该模式下不走鼠标坐标）
watch(isLookingAway, () => render())

onBeforeUnmount(() => {
  window.removeEventListener('mousemove', handleMouseMove)
  if (frameId) {
    cancelAnimationFrame(frameId)
    frameId = 0
  }
  resizeObserver?.disconnect()
  resizeObserver = null
  eyes = []
})
</script>

<style lang="less" scoped>
/*
 * 配色原则：
 * - 插画背景走全局主题变量（base.css / base.dark.css），深浅主题自动翻转；
 * - 眼皮与眼缝必须用角色自身的红（比脸略暗），而不是主题色——
 *   它们要落在红色的脸上，用主题色会露出异色斑块。
 */
.mouse-eyes {
  position: absolute;
  inset: 0;
  overflow: hidden;
  /* 装饰层不参与事件，避免遮挡左侧区域的鼠标交互 */
  pointer-events: none;
  /* 浅色下是近白的淡蓝，深色下是深蓝黑，两侧都与右侧表单卡片形成层次 */
  background: var(--main-50);
  display: flex;
  align-items: flex-end;
  justify-content: center;
}

/*
 * 走动舞台：尺寸严格等于图片显示区域（眼睛的百分比定位才是相对图片的），
 * 高度取左区高度的 78%，宽度由素材比例（580×883，帽子到腰的半身）推出。
 * 高度是整个组件的「标尺」：素材高度不变 ⇒ 角色显示大小与瞳孔可移动幅度都不变，
 * 因此加宽画布（容纳叉腰的双臂）只改这里的宽度比，不要动 height。
 * 不要再缩小：角色变小时眼睛同步变小，瞳孔可移动的像素量随之被压到看不见跟随
 * （全身版实测 88% 时只有约 2px 位移，屏幕上几乎察觉不到）。
 * transform 由 JS 写（平移 + 倾斜），origin 在底部中心，倾斜时像绕脚旋转。
 */
.stage {
  position: relative;
  height: 78%;
  width: auto;
  aspect-ratio: 580 / 883;
  flex: 0 0 auto;
  transition: transform 0.45s ease-out;
  transform-origin: bottom center;
}

.hero-img {
  display: block;
  width: 100%;
  height: 100%;
}

/*
 * 眼睛定位：按素材量取的像素坐标换算成图片百分比（底图为 580×883）。
 * 瞳孔中心为左 (500, 528)、右 (656, 546)（源素材坐标），
 * 换算后左眼落在 33.3% / 48.7%、右眼落在 60.2% / 50.7%。
 * 两眼**不同高**：角色头略向画面左侧转、右眼更近镜头，所以右眼比左眼低 2 个百分点，
 * 不要为了「对称好看」把它们调成同一 top（那样瞳孔会浮在眼白外）。
 * 改素材裁剪范围或瞳孔坐标（prepare_stand.py 的 BOX / EYES_SRC）时必须同步重算，
 * prepare_stand.py 结尾会直接打印这组百分比，抄它就行，不要手算。
 * 宽度 17.2% = 眼球框 100px / 图宽 580px：框架按实测眼白直径取，
 * 瞳孔就在这个框内移动，超出去就会压到眼睑。
 */
.eye {
  position: absolute;
  width: 17.2%;
  aspect-ratio: 1;
  // 素材带透明留白，裁剪层保证瞳孔任何方向都不会绘制到眼睛范围外。
  overflow: hidden;
  border-radius: 50%;
  transform: translate(-50%, -50%);
}

.eye.is-right {
  left: 60.2%;
  top: 50.7%;
}

.eye.is-left {
  left: 33.3%;
  top: 48.7%;
}

/*
 * 瞳孔贴图：尺寸基准是父元素 .eye（眼球框），不是整张图 ——
 * 贴图 82px / 眼球框 100px = 82%，必须按这个比例写死。
 * 写成 100% 会把贴图拉大到眼球框尺寸，瞳孔随之被放大，
 * 可移动半径（框宽与瞳孔显示直径之差的一半）会被吃掉一半以上，跟随便看不出来（踩过）。
 * 用 inset + margin: auto 居中，JS 写 transform 时不必再拼一段居中位移。
 * 可偏移半径由 data-core（瞳孔本体占贴图的比例）动态算出，见 script 的 measureEyes。
 */
.pupil {
  position: absolute;
  inset: 0;
  margin: auto;
  width: 82%;
  height: 82%;
  transition: transform 0.1s ease-out;
  will-change: transform;
}



/* 系统开启「减少动态效果」时去掉所有补间，只保留状态切换的瞬时变化 */
@media (prefers-reduced-motion: reduce) {
  .mouse-eyes .stage,
  .mouse-eyes .pupil {
    transition: none;
  }
}
</style>
