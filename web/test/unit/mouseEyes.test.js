import assert from 'node:assert/strict'
import test from 'node:test'

import { calcPupilOffset } from '../../src/utils/mouseEyes.js'

// 眼球框取实测值：角色缩到容器高度 42% 时单眼框只有 27×27 CSS px
const EYE = { left: 400, top: 650, width: 27, height: 27 }
const CENTER_X = EYE.left + EYE.width / 2
const CENTER_Y = EYE.top + EYE.height / 2

test('纵向可移动半径被安全边距吃光时瞳孔不能飞出眼球', () => {
  // 负向场景：27px 的眼球框纵向余量只有约 4.6px，扣掉两个安全边距后 maxY 落到 0。
  // 此时「鼠标正对眼睛上方/下方」必须让瞳孔停在眼球中心，而不是跟着鼠标跑出去——
  // 旧实现把 maxY=0 当成「该轴不参与归一化」，会返回 y=-660px（眼球框仅 27px），
  // 肉眼看到的是整只眼只剩眼白，瞳孔彻底消失。
  const up = calcPupilOffset(CENTER_X, 0, EYE, 3.65, 0)
  assert.deepEqual(up, { x: 0, y: 0 })

  const down = calcPupilOffset(CENTER_X, 1000, EYE, 3.65, 0)
  assert.deepEqual(down, { x: 0, y: 0 })

  // 斜向也是同理：纵向分量必须被压回 0，横向仍可正常贴边
  const diagonal = calcPupilOffset(CENTER_X + 800, 0, EYE, 3.65, 0)
  assert.equal(diagonal.y, 0)
  assert.equal(diagonal.x, 3.65)
})

test('横向与纵向都受限时，位移不超出各自的可移动半径', () => {
  // 正向场景：42% 尺寸下的实际入参（maxX 3.65 / maxY 1.65）
  const corners = [
    [0, 0],
    [1600, 0],
    [0, 1000],
    [1600, 1000]
  ]
  corners.forEach(([mx, my]) => {
    const p = calcPupilOffset(mx, my, EYE, 3.65, 1.65)
    assert.ok(Math.abs(p.x) <= 3.65 + 1e-9, `x 溢出: ${p.x}`)
    assert.ok(Math.abs(p.y) <= 1.65 + 1e-9, `y 溢出: ${p.y}`)
  })
})

test('鼠标位于眼球中心时瞳孔回到原点', () => {
  assert.deepEqual(calcPupilOffset(CENTER_X, CENTER_Y, EYE, 3.65, 1.65), { x: 0, y: 0 })
})

test('一个方向可动、另一个方向为 0 时互不影响', () => {
  // 纵向不可动不妨碍横向跟随（只带纵向分量的位移不能倒灌回横向）
  const left = calcPupilOffset(0, CENTER_Y, EYE, 3.65, 0)
  assert.equal(left.x, -3.65)
  assert.equal(left.y, 0)
})
