/**
 * 登录页插画「眼睛跟随鼠标」的几何计算与环境探测。
 *
 * 为什么单独抽成纯函数：瞳孔偏移的边界行为（鼠标正好压在眼球中心、鼠标远在
 * 视口之外）无法在仓库现有的无 DOM 单测环境里用真实鼠标事件覆盖，抽成纯函数后
 * 可以直接断言数值结果。组件侧只负责读取眼球包围盒、写入 transform。
 */

/**
 * 计算瞳孔相对眼球中心的位移量（px）。
 *
 * 做法与常见实现一致：先用 atan2 取「眼球中心 → 鼠标」的方向角，
 * 再用截断到 maxDistance 的距离作为向量长度，得到既有方向又有上限的偏移。
 *
 * @param {number} mouseX 鼠标视口坐标 X（event.clientX）
 * @param {number} mouseY 鼠标视口坐标 Y（event.clientY）
 * @param {{left: number, top: number, width: number, height: number}} eyeRect 眼球在视口中的包围盒，
 *   必须来自元素自身的 getBoundingClientRect()——元素的 transform 会改变包围盒，因此不能缓存跨帧复用
 * @param {number} maxDistance 瞳孔允许偏离眼球中心的最大距离，用于把瞳孔限制在眼眶内
 * @returns {{x: number, y: number}} 位移量；鼠标恰好位于眼球中心时为 {x: 0, y: 0}
 */
export function calcPupilOffset(mouseX, mouseY, eyeRect, maxDistance, maxY = maxDistance) {
  const deltaX = mouseX - (eyeRect.left + eyeRect.width / 2)
  const deltaY = mouseY - (eyeRect.top + eyeRect.height / 2)
  // 距离先截断、再乘方向：等价于把鼠标向量投影到半径 maxDistance 的圆内，
  // 保证鼠标跑得再远瞳孔也不会溢出眼眶。
  // 用 sqrt 而非 Math.hypot：hypot 带溢出保护、在 V8 上慢一个量级，这里每帧调用，取 sqrt。
  // 宽高分别限制，避免旋转后的包围盒让上下方向可移动距离被高估。
  const normalizedX = maxDistance > 0 ? deltaX / maxDistance : 0
  const normalizedY = maxY > 0 ? deltaY / maxY : 0
  const scale = Math.min(1, 1 / Math.sqrt(normalizedX * normalizedX + normalizedY * normalizedY))
  // deltaX 与 deltaY 同时为 0 时 atan2 返回 0，distance 也是 0，结果自然落在原点，无需额外分支
  return {
    x: deltaX * scale,
    y: deltaY * scale
  }
}

/**
 * 当前环境是否要求关闭动效（系统「减少动态效果」偏好）。
 *
 * 命中时调用方应停止鼠标跟随与眨眼调度，只保留静态插画；
 * 首次渲染（SSR 或样式未就绪）读不到 matchMedia 时按「不需要关闭」处理，避免误伤正常用户。
 */
export function prefersReducedMotion() {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return false
  }
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/**
 * 当前环境的主指针是否为粗指针（触摸屏）。
 *
 * 触摸设备没有持续的 mousemove，也不会因为缺少跟随而显得「坏了」，
 * 因此调用方应跳过事件绑定，省掉无意义的监听与每帧布局读取。
 */
export function isCoarsePointer() {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return false
  }
  return window.matchMedia('(pointer: coarse)').matches
}
