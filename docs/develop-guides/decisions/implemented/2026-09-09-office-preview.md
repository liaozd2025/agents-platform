# 扩展六种 Office 格式预览

状态：implemented
类型：feature
Owner：backend/package/yuxi/utils/filepreview.py

## 问题

现有前后端预览分类仅支持 `.docx`、`.pptx`，通过后端转换为 PDF 展示。用户需要阅读 `.doc`、`.docx`、`.ppt`、`.pptx`、`.xls`、`.xlsx` 六种格式；上传或知识解析支持不能代替预览支持。

## 决策

已确认格式范围为上述六种，覆盖工作区、会话文件、Agent 产物和知识库原文件的现有预览入口。Excel 使用只读表格，支持全部可见工作表切换、滚动查看单元格，保留单元格内容、已保存的公式结果、合并单元格和基本格式。缺少已保存公式结果时明确提示；编辑、重新计算公式、图表、图片和复杂排版不在本次验收范围。

已确认的异常与展示边界：Word 和 PowerPoint 沿用静态 PDF 预览，不播放动画、音视频；保留 30 MB 文件上限，并对 Excel 展开规模设置有界保护；加密、损坏、超限或转换失败时说明原因并保留原文件下载，避免静默截断内容。

通用格式与转换由 `backend/package/yuxi/utils/filepreview.py` 拥有，工作区与知识库保留各自存储、授权和缓存边界，遵循[既有 Owner 决策](2026-08-21-preview-owner-separation.md)。前端格式归一化由 `web/src/utils/file_preview.js` 拥有；`SpreadsheetPreview.vue` 使用原生表格渲染，后端 openpyxl 读取 XLSX、xlrd 读取 XLS，均只使用原文件保存值。Excel 解析在资源受限的独立子进程中执行。

## 替代方案

- 六种格式统一转 PDF：复用现有展示链路，但不满足已确认的 Excel 工作表切换与连续表格阅读需求。
- Excel 只读表格：满足已确认的阅读方式，需要新增表格渲染与对应数据契约。

## 后果

Excel 已保存的公式结果可能与外部数据不同步，预览按文件保存状态展示。压缩文件大小不能代表工作簿展开规模，需限制解析和渲染资源。基本格式范围为粗体、斜体、字体色、填充色、水平对齐、常用十进制、千分位、百分数和货币；日期显示 ISO 值，复杂自定义数字格式显示原值并保留格式提示。预览拒绝超过 30 MB 的原文件、64 MB 的 XLSX 解压总量、10000 个 ZIP 项、100000 个可见表格位置或 16 MB 的响应内容。原生表格合并跨度最多 1000 列、65534 行，超出时拒绝预览以防浏览器静默错列。Excel 解析受 1 GiB 地址空间、20 秒 CPU 和 30 秒墙钟时间约束，超限明确失败。

## 验证

使用隔离的 `office-preview` Compose 项目，真实 PostgreSQL、Redis、MinIO 和 LibreOffice；未改动现有业务服务。以下容器命令均带 `COMPOSE_FILE=office-preview.local.yaml COMPOSE_PROJECT_NAME=office-preview`。测试容器依赖已安装，因此使用 `--no-sync`；锁文件通过 `uv lock --check --offline`。

| 验收主张 | 证据 | 结果 |
|---|---|---|
| 六种格式在全部现有入口可预览且保留原文件 | `docker compose exec -T -e LITE_MODE=false -e TEST_USERNAME=... -e TEST_PASSWORD=... api uv run --no-sync --group test pytest test/integration/api/test_office_preview_api.py -q` | 6 passed；24 次真实预览，核对 PDF 正文、Excel 内容、原文件下载字节及匿名拒绝 |
| Excel 保留可见工作表、合并、格式和保存值，不重新计算 | `docker compose exec -T api uv run --no-sync --group test pytest test/unit/utils/test_spreadsheet_preview.py -q` | 9 passed；XLS/XLSX 保存公式值故意不同于运算结果，断言按保存值展示；隐藏表排除、缺失缓存明确提示 |
| 损坏和超限明确失败、没有部分内容 | 同上，真实损坏文件、稀疏网格、压缩膨胀、ZIP 项数、响应大小、两维合并跨度和超时负例 | 全部通过；格式白名单与合并跨度回归均观察到先红后绿 |
| 普通与全屏表格可操作 | 浏览器加载真实 `AgentFilePreview` 的临时组件验收页，使用后端生成数据 | 普通/全屏切换工作表通过；加载、空表、超限及下载按钮保留通过；深浅色视觉检查通过；390px 窄屏键盘滚动，scrollTop=1071、scrollLeft=40，末行 50 可见 |
| 前端与文档交付可构建 | `docker compose exec -T web pnpm run test:unit`、`pnpm run lint:check`、`pnpm run build`；隔离容器中 docs `pnpm run build` | 前端 238 passed，lint、前端 build、docs build 通过；构建仍提示已有大 chunk |
| Python 格式与工程约束回归 | 改动文件 Ruff check / format check；`python3 -m unittest scripts.test_verify_engineering_contracts`；`git diff --check` | 通过；工程 verifier 单测 61 passed |

最终后端全量命令：`docker compose exec -T -e LITE_MODE=false api uv run --no-sync --group test pytest test/unit -m "not slow"`，结果为 **1889 passed、45 skipped、6 failed**。失败为 LITE import 边界 1 项与 `test_chat_service_langfuse_stream.py` 5 项。独立复制原始提交 `5935b369` 在同容器执行后，六项同名失败全部复现；基线另有四项 Compose 边界失败（基线合计 10 failed），当前容器未挂载仓库根 Compose 文件，相关测试按现有规则跳过，不能宣称这些边界通过。

`python3 scripts/verify_engineering_contracts.py` 仍报告 **12 项已有问题**；原始提交独立 worktree 得到完全相同结果，涉及 dashboard-display-name、dashscope-default-models、oa-account-login-account-only、knowledge-retrieval-routing 旧决策记录。此次 Office 记录未新增 gate 错误。测试与固定六格式 fixture 已入库，可按上述命令复核。

浏览器验证覆盖真实组件交互，HTTP 集成覆盖四入口；未执行完整产品登录后的点击链路、Agent 生成过程或知识库向量索引，不把这些范围声明为通过。未部署或推送。
