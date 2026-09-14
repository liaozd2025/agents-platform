import { ref } from 'vue'
import { defineStore } from 'pinia'
import { theme } from 'ant-design-vue'

// 读取不到 CSS 变量时的兜底主色（与 base.css 的 --main-600 同值）。
// 正常运行时不会命中，只在样式尚未加载的极端时序下避免 antd 拿到空值而回退到默认蓝。
const FALLBACK_PRIMARY = '#5263e7'

// 深色模式下压在主色实底上的文字色（近黑）。取值理由见 buildTheme 内注释。
const DARK_SOLID_TEXT = '#0b0d10'

// 深色背景三层的兜底值（与 base.dark.css 的 --gray-0 / --gray-50 / --gray-100 同值）。
// 同样只在样式尚未加载的极端时序下命中。
const FALLBACK_DARK_BG = { base: '#151517', container: '#1b1b1c', elevated: '#212123' }

/**
 * 从 base.css / base.dark.css 读取一个 CSS 变量的当前计算值。
 *
 * 为什么不直接把 'var(--x)' 写进 token：
 *   antd 会基于这些颜色派生出 hover/active/浅底/描边等一整套色值，
 *   拿到 var() 字符串无法参与颜色运算，会算出一批无效色值。
 * 为什么切换主题后能立刻读到新值：
 *   getComputedStyle 会强制同步触发样式重算，只要 :root.dark 类已经加上，
 *   这里读到的就是新主题的变量值。
 */
function readVar(name, fallback) {
  if (typeof window === 'undefined') return fallback
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}

export const useThemeStore = defineStore('theme', () => {
  // 从 localStorage 读取保存的主题，默认为浅色
  const isDark = ref(localStorage.getItem('theme') === 'dark')

  /**
   * 按当前模式构建 antd 主题配置。
   *
   * 主色每次现算（浅色取 --main-600、深色取深色表的 --main-600），
   * 不再像以前那样在 token 里写死一个十六进制值 —— 那样会让 antd 组件与
   * 自研样式各存一份主色真值，两者一旦漂移，页面上就会同时出现两种蓝。
   */
  function buildTheme(dark) {
    return {
      token: {
        fontFamily:
          "'HarmonyOS Sans SC', Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, 'Fira Sans', 'Droid Sans', 'Helvetica Neue', sans-serif;",
        colorPrimary: readVar('--main-color', FALLBACK_PRIMARY),
        colorLink: 'var(--main-color)',
        colorLinkHover: 'var(--main-600)',
        colorLinkActive: 'var(--main-800)',
        borderRadius: 8,
        wireframe: false,
        // 深色底三层必须显式对齐自研灰阶：darkAlgorithm 默认从纯黑派生整套中性色
        // （底色 #000、容器 #141414），不覆盖的话 antd 组件（卡片、表格、下拉、弹窗）
        // 会明显比自研区域更黑，同一屏上出现两种深色。
        // colorBgBase 是 seed token，改它会连带重新派生边框、次级文本等中性色，
        // 这正是「整体抬亮」想要的效果，但容器/浮层仍显式钉在与 --gray-* 相同的档位，
        // 避免 antd 的派生结果与自研变量对不上。
        ...(dark
          ? {
              colorBgBase: readVar('--gray-0', FALLBACK_DARK_BG.base),
              colorBgContainer: readVar('--gray-50', FALLBACK_DARK_BG.container),
              colorBgElevated: readVar('--gray-100', FALLBACK_DARK_BG.elevated)
            }
          : {})
      },
      // 深色模式套用 antd 的暗色算法，浅色模式不传 algorithm（走默认算法）
      ...(dark
        ? {
            algorithm: theme.darkAlgorithm,
            // 深色下表里的 --main-600 是亮色（#8692f0），而 antd 的实心主按钮文字
            // 恒为纯白（源码 theme/util/alias.ts: `colorTextLightSolid: mergedToken.colorWhite`），
            // 亮底白字只有 2.8:1 —— 按钮字会发糊。这里只对 Button 组件把实心文字
            // 改成近黑（6.5:1），与自研按钮「--main-color 配 --gray-0」的做法一致。
            //
            // 为什么不改全局 token：colorTextLightSolid 是全局 alias token，
            // Tooltip 的深底白字、Badge 计数等也依赖它，全局改会造成深底黑字。
            // 组件级 override 在 formatToken 的 aliasToken 之后展开，可以安全覆盖。
            components: { Button: { colorTextLightSolid: DARK_SOLID_TEXT } }
          }
        : {})
    }
  }

  // 先把 :root.dark 类落到 document 上，再构建初始配置。
  // 反过来的话，深色偏好的用户首屏会读到浅色主题的 --main-color，出现一次主色跳变。
  updateDocumentTheme()

  // 当前主题配置
  const currentTheme = ref(buildTheme(isDark.value))

  // 切换主题
  function toggleTheme() {
    setTheme(!isDark.value)
  }

  // 设置主题
  function setTheme(dark) {
    isDark.value = dark
    localStorage.setItem('theme', dark ? 'dark' : 'light')
    // 必须先切换 :root.dark 类、再重建配置：
    // buildTheme 依赖 CSS 变量，顺序颠倒会读到上一个主题的 --main-color
    updateDocumentTheme()
    currentTheme.value = buildTheme(dark)
  }

  // 更新 document 的主题类
  function updateDocumentTheme() {
    if (isDark.value) {
      document.documentElement.classList.add('dark')
    } else {
      document.documentElement.classList.remove('dark')
    }
  }

  return {
    isDark,
    currentTheme,
    toggleTheme,
    setTheme
  }
})
