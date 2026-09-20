# 登录页视觉改版：整屏背景、半透明表单层与交互式角色插画

状态：implemented
类型：feature
Owner：web/src/views/LoginView.vue（版式与状态接线）、web/src/components/MouseEyesCharacter.vue（插画组件）

## 问题

登录页原为浅色纯背景 + 白色卡片 + 左侧静态图片，观感平淡且与品牌素材（九典红色角色）缺乏关联；登录行为提示（如输入密码）没有任何界面反馈载体。

## 决策

1. 登录页整屏使用品牌渐变背景图（`web/public/login-page-background.png`，1920×1080，`center center / cover` 铺满视口），背景无主体元素，任意窗口比例裁切都不损失信息。
2. 登录卡片本身透明化，表单侧使用半透明白（rgba(255,255,255,0.78)）+ `backdrop-filter: blur(10px)`，在透出背景的同时保证表单文字可读性。
3. 左侧插画替换为 `MouseEyesCharacter` 交互组件：角色瞳孔跟随鼠标；密码框聚焦、有输入或切明文时瞳孔移开视线，失焦或清空后恢复跟随。几何计算与环境探测抽为纯函数 `web/src/utils/mouseEyes.js`（无 DOM 单测可直接断言数值）。
4. 降级边界：系统「减少动态效果」偏好命中时停止跟随与眨眼；粗指针（触摸屏）设备跳过 mousemove 监听；≤768px 窗口沿用既有规则整体隐藏插画区。
5. 瞳孔贴图与角色位图的裁剪坐标按素材绝对像素标定（组件内 `BOX`/`EYES_SRC` 常量组），更换素材必须整组重标，不允许局部覆盖。

## 替代方案

- 纯 CSS 渐变背景：无品牌素材承载能力，且后续换品牌图仍需改代码。
- SVG/Lottie 动画角色：制作成本高，与现有位图素材（抠图半身像 + 瞳孔贴图）不匹配。

## 后果

- 新增 4 个 public 位图资源（背景 + 角色半身像 + 左右瞳孔贴图），首屏登录页加载体积增加约 0.7 MB；登录页为低频页面，可接受。
- 背景图在超宽屏（>16:9）会上下裁切、竖窗会左右裁切；渐变无主体，视觉无损。
- 「保持登录 30 天」勾选替代原《用户协议》《隐私协议》同意入口的行为变化，见[保持登录决策](2026-09-12-remember-login-30-days.md)，已获合规确认。

## 验证

- `eslint`（LoginView.vue、MouseEyesCharacter.vue、mouseEyes.js、stores/user.js）通过；`vite build` 通过。
- `node --test test/unit/mouseEyesOffset.test.js test/unit/authNavigation.test.js test/unit/authSessionAbort.test.js` 全部通过。
- 本地构建产物 + Playwright 截图（1440×900，浅色主题）：默认态与密码输入态渲染正常。
