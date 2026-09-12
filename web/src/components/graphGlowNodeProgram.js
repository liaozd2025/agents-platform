import { NodeProgram } from 'sigma/rendering'
import { floatColor } from 'sigma/utils'

/**
 * 「浅色全息」节点着色器：核心实心圆 + 光环 + 径向衰减光晕。
 *
 * 为什么要自己写着色器，而不是用 @sigma/node-border
 * ------------------------------------------------------------------
 * node-border 画的同心圆盘每一层都是**硬边界**（只有 1px 抗锯齿过渡），
 * 叠出来的效果是"外面套了几个圈"，而不是"光散出去"。浅色底上尤其明显：
 * 实测三层同心圆看上去像靶环，完全没有发光的观感。真正的辉光需要按距离
 * 做连续衰减，这只能落在片元着色器里。
 * sigma 官方把节点程序开放为扩展点（@sigma/node-border 就是基于它实现的），
 * 因此这里继承官方的 NodeProgram 基类自己实现，没有改动 sigma 源码。
 *
 * 几何
 * ------------------------------------------------------------------
 * 每个节点铺 6 个顶点（两个三角形拼成的正方形），用常量属性 a_corner
 * 传递 [-1,1]² 的角坐标。sigma 的坐标习惯是「顶点落在半径 size 处、
 * v_radius = size/2 是能被完整覆盖的圆」，所以正方形内切圆半径 = v_radius，
 * 与默认 NodeCircleProgram 的口径一致（默认程序用三角形拼，内切圆同样是 v_radius）。
 * 片元里用 length(v_corner) 得到归一化距离，超出 1 的地方直接 discard，
 * 于是正方形被裁成正圆。
 *
 * 尺寸口径
 * ------------------------------------------------------------------
 * a_size 到屏幕像素的换算与 sigma 默认节点程序完全相同（同一套
 * u_correctionRatio / u_sizeRatio 公式），所以调用方只要把节点 size 乘上
 * GLOW_SCALE，核心圆半径就与改动前一致，多出来的空间全部让给光晕。
 * 调用方必须让渲染尺寸 = size × GLOW_SCALE，两个数字是一组，不能单独改。
 *
 * 颜色必须由调用方预乘
 * ------------------------------------------------------------------
 * sigma 使用 gl.blendFunc(ONE, ONE_MINUS_SRC_ALPHA)（预乘混合），而它的顶点
 * 着色器把 a_color 原样直通、不做预乘。因此片元输出前必须自己乘 alpha
 * （见下方 gl_FragColor = vec4(v_color.rgb * a, a)），否则半透明区域会以
 * 满亮度盖上去。节点属性里的 color 本身仍按原始色传入。
 */

export const GLOW_RING_NODE_TYPE = 'glowRing'

/** 光晕外沿倍率：光晕从核心边缘一直衰减到核心半径的 GLOW_SCALE 倍。 */
export const GLOW_SCALE = 3.0

export const VERTEX_SHADER_SOURCE = /* glsl */ `
attribute vec2 a_position;
attribute float a_size;
attribute vec2 a_corner;

#ifdef PICKING_MODE
attribute vec4 a_id;
#else
attribute vec4 a_color;
#endif

uniform mat3 u_matrix;
uniform float u_sizeRatio;
uniform float u_correctionRatio;

varying vec2 v_corner;
varying vec4 v_color;

const float bias = 255.0 / 254.0;

void main(void) {
  float size = a_size * u_correctionRatio / u_sizeRatio * 4.0;
  vec2 diffVector = a_corner * (size / 2.0);
  vec2 position = a_position + diffVector;
  gl_Position = vec4((u_matrix * vec3(position, 1)).xy, 0, 1);

  v_corner = a_corner;

  #ifdef PICKING_MODE
  v_color = a_id;
  #else
  v_color = a_color;
  #endif
  v_color.a *= bias;
}
`

export const FRAGMENT_SHADER_SOURCE = /* glsl */ `
precision highp float;

varying vec2 v_corner;
varying vec4 v_color;

// 光晕外沿倍率，必须与 JS 侧的 GLOW_SCALE 一致
const float GLOW_SCALE = ${GLOW_SCALE.toFixed(1)};

void main(void) {
  float d = length(v_corner);
  // 把正方形裁成正圆
  if (d > 1.0) discard;

  // t = 1 恰好落在核心圆边缘
  float t = d * GLOW_SCALE;

  #ifdef PICKING_MODE
  // 命中区域只认核心圆，避免把外面一大圈光晕都算成可点击区域
  if (t > 1.0) discard;
  gl_FragColor = v_color;
  #else
  // 核心：实心，边缘轻微柔化
  float core = 1.0 - smoothstep(0.80, 1.0, t);
  // 光环：紧贴核心外沿的一圈亮带，负责 HUD 的目标标记感。
  // 权重刻意压得比光晕低——环一旦过亮就会变成"甜甜圈"，盖过发光的整体观感。
  float ring = smoothstep(1.02, 1.18, t) * (1.0 - smoothstep(1.28, 1.50, t));
  // 光晕：从核心边缘向外连续衰减，这是"发光"观感的来源。
  // 指数 1.4 是实测出来的：2 以上衰减太快，浅色底上几乎看不见；1.4 才有"雾"的体量。
  float halo = pow(max(0.0, 1.0 - t / GLOW_SCALE), 1.4);

  float a = core * 0.95 + ring * 0.45 + halo * 0.7;
  a = clamp(a, 0.0, 1.0) * v_color.a;

  // 预乘输出：sigma 走 ONE / ONE_MINUS_SRC_ALPHA
  gl_FragColor = vec4(v_color.rgb * a, a);
  #endif
}
`

/** 正方形两个三角形：角坐标顺序必须与下面的 CONSTANT_DATA 严格一致。 */
const CORNER_TOP_LEFT = [-1, -1]
const CORNER_TOP_RIGHT = [1, -1]
const CORNER_BOTTOM_LEFT = [-1, 1]
const CORNER_BOTTOM_RIGHT = [1, 1]

export class GlowRingNodeProgram extends NodeProgram {
  getDefinition() {
    return {
      VERTICES: 6,
      VERTEX_SHADER_SOURCE,
      FRAGMENT_SHADER_SOURCE,
      METHOD: WebGLRenderingContext.TRIANGLES,
      UNIFORMS: ['u_sizeRatio', 'u_correctionRatio', 'u_matrix'],
      ATTRIBUTES: [
        { name: 'a_position', size: 2, type: WebGLRenderingContext.FLOAT },
        { name: 'a_size', size: 1, type: WebGLRenderingContext.FLOAT },
        {
          name: 'a_color',
          size: 4,
          type: WebGLRenderingContext.UNSIGNED_BYTE,
          normalized: true
        },
        {
          name: 'a_id',
          size: 4,
          type: WebGLRenderingContext.UNSIGNED_BYTE,
          normalized: true
        }
      ],
      CONSTANT_ATTRIBUTES: [
        { name: 'a_corner', size: 2, type: WebGLRenderingContext.FLOAT }
      ],
      CONSTANT_DATA: [
        CORNER_TOP_LEFT,
        CORNER_TOP_RIGHT,
        CORNER_BOTTOM_LEFT,
        CORNER_TOP_RIGHT,
        CORNER_BOTTOM_RIGHT,
        CORNER_BOTTOM_LEFT
      ]
    }
  }

  processVisibleItem(nodeIndex, startIndex, data) {
    const array = this.array
    // 顺序必须与 ATTRIBUTES 声明一致：position(2) → size(1) → color(1) → id(1)
    array[startIndex++] = data.x
    array[startIndex++] = data.y
    array[startIndex++] = data.size
    array[startIndex++] = floatColor(data.color)
    // 末位不递增：startIndex 是入参、调用方不回读，递增会触发 no-useless-assignment
    array[startIndex] = nodeIndex
  }

  setUniforms(params, { gl, uniformLocations }) {
    const { u_sizeRatio, u_correctionRatio, u_matrix } = uniformLocations
    gl.uniform1f(u_correctionRatio, params.correctionRatio)
    gl.uniform1f(u_sizeRatio, params.sizeRatio)
    gl.uniformMatrix3fv(u_matrix, false, params.matrix)
  }
}
