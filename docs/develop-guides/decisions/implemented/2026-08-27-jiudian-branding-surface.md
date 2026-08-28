# 九典品牌替换默认产品入口

状态：implemented
类型：feature
Owner：backend/package/yuxi/config/static/info.template.yaml

## 问题

默认配置、浏览器标题、登录页和用户菜单混用“语析”、Yuxi 及上游文档入口，公开根路由也展示上游项目首页，九典部署无法形成一致的产品身份。

## 决策

默认信息配置统一使用“九典制药”和九典 Logo，浏览器标题与 favicon 使用同一品牌资源。公开根路由直接进入 `/agent`，由现有认证守卫决定进入工作区或登录页；用户菜单移除“文档中心”入口，侧边栏品牌仅展示身份；登录页在组织名与品牌名相同时只显示一次名称。

应用内运行时品牌信息仍由 `info.template.yaml` 及其现有本地覆盖机制拥有，路由和组件只消费该配置；加载运行时配置前的浏览器标题与 favicon 由 `web/index.html` 独立拥有，不受 `info.local.yaml` 覆盖。认证、OA 嵌入、任务中心、权限导航和业务路由保持现有契约。

## 替代方案

- 只修改默认信息配置：页面仍会保留上游入口和公开营销首页，品牌身份不完整。
- 在各组件硬编码九典名称：会绕过现有信息配置 Owner，并让自定义部署失去覆盖能力。
- 保留公开首页并改写全部营销内容：当前没有九典营销首页需求，会扩大文案、数据和维护范围。

## 后果

未登录访问 `/` 会按现有 `/agent` 认证守卫进入登录流程，不再展示公开营销首页。自定义 `info.local.yaml` 仍可覆盖应用内默认品牌，但不会覆盖静态浏览器标题与 favicon。用户菜单的上游文档入口和可点击品牌链接不再提供；功能弹窗、登录页等场景中的上下文帮助链接不属于本次品牌入口清理。未使用的首页组件暂时保留，不扩大本次范围。

## 验证

- `cd web && pnpm run lint:check`：通过。
- `cd web && pnpm run test:unit`：包含根路由和登录页品牌负向检查；88 项通过。
- `cd web && pnpm run build`：通过；仅有既有 chunk 大小警告。
- 当前分支 Vite 服务回读：`/` HTML 包含九典标题与 favicon，Logo 请求返回 `200 image/png`。
- `python3 -m unittest scripts.test_verify_engineering_contracts`：62 项通过；`git diff --check` 通过。
- `python3 scripts/verify_engineering_contracts.py`：被主线已有的 `2026-08-25-folder-upload-hierarchy.md` 无效 Owner 引用阻断，本次未修改该文件。
- `cd docs && pnpm run build`：被主线已有的 `issue-tracker.md` 和 `domain.md` 共 3 个死链阻断，本次未修改这些文件。
- 真实浏览器登录页、认证后侧边栏、浅色/暗色和移动端截图未执行：当前环境未连接任何可用浏览器，PR 将保留该未验证范围。
