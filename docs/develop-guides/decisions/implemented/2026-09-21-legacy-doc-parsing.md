# 接通旧版 Word 文档解析

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/knowledge/parser/unified.py

## 问题

知识库格式声明拒绝 `.doc`，聊天附件虽然能保存原件，但 Agent 解析工具进入统一解析器后也会失败。锁定的 Docling Slim 2.122.0 与后端镜像中的 LibreOffice Writer 已具备旧版 Word 转换能力。

## 决策

格式声明开放 `.doc`，统一解析器使用 `InputFormat.DOC` 和现有 Word backend。转换、120 秒超时、独立 LibreOffice profile 与临时文件清理由 Docling 负责，原始 DOC 不进入仅支持 DOCX 的 python-docx 回退。工具说明向模型声明 DOC 可直接传入。

知识库按现有流程解析、分块和索引。聊天附件保持原件上传、Agent 按需解析，上传弹窗的预解析仍仅支持 PDF 和图片。格式声明、转换分派、工具说明和附件服务分别拥有这些行为；操作说明见[文档处理与 OCR](../../../advanced/document-processing.md)。

## 替代方案

- 自行实现 DOC 转 DOCX：与锁定依赖已有能力重复，不采用。
- 转 PDF 后 OCR：增加渲染与识别步骤，不作为普通 Word 解析路径。
- 扩展附件弹窗预解析：用户选择按需解析，不扩展交互。

## 后果

后端执行进程需要 LibreOffice Writer，现有 API/worker 镜像已声明安装。无需升级 Python 依赖、增加配置或更改数据结构。后端 unit workflow 显式安装 LibreOffice Writer 和 Calc，使固定 DOC 回归及检测到 LibreOffice 后启用的既有 XLS 回归在 CI 中执行真实转换。Markdown 是内容提取结果，不承诺复杂排版、图片文字识别、模板保真或加密文件可读。

## 验证

固定样本 `backend/test/data/legacy_word.doc` 由人工指定的 RTF 正文和两行两列表格经 LibreOffice 的 `MS Word 97` 过滤器生成，包含中文正文、标识 `DOC-ROUNDTRIP-2026`、产品及数量 42；无业务数据。样本生成不在测试或 CI 内执行。测试核对 OLE2 签名、独立指定的正文和 Markdown 表格、原件字节；负向样本截断为 512 字节，必须明确失败且不能触发 DOCX 回退。

- 独立 Compose 容器挂载当前分支，运行 `python -m pytest test/unit/knowledge/test_parser_capabilities.py test/unit/knowledge/test_parser_facade.py test/unit/toolkits/test_ocr_parse_file_tool.py test/unit/services/test_attachment_service.py test/unit/routers/test_knowledge_router_cleanup.py test/unit/routers/test_knowledge_workspace_import.py -q`：92 passed。工具测试覆盖主、子 Agent 执行过滤、真实 DOC 解析与结果内容，文件系统边界使用测试替身。
- 临时 Uvicorn 进程装配真实知识库与聊天路由，身份依赖替换为固定测试身份，连接独立真实 MinIO：HTTP 知识库 DOC 上传后回读原件，并从对象路径解析出正文及表格；HTTP Markdown 接口返回内容；不支持的 PPT 上传返回 400；聊天 DOC 上传保持 `parse_supported=false`、原件字节不变，预解析请求返回 400。临时对象均删除。
- 上述 HTTP 检查未覆盖真实登录与权限、数据库文件状态、worker 向量入库检索或真实沙盒和模型自动选择工具；这些完整业务 E2E 未执行：本次独立环境只装配路由、解析器和 MinIO，未装配 PostgreSQL/worker/向量库、沙盒和模型凭据；变更不涉及这些下游流程，当前证据仅覆盖格式开放与正文转换。未部署生产。
- 工程信任检查通过，其 unittest 为 62 passed；修改文件 Ruff lint、format 与 `git diff --check` 通过。独立 Reviewer 指出的工具说明冲突已修复，工具 10 项回归重新通过。
- 文档依赖按锁文件安装并 hoist 后，`node docs/node_modules/vitepress/bin/vitepress.js build docs` 通过（含相对链接检查）；独立复审确认无剩余问题。临时 Compose 服务、网络和测试对象均已清理。

提交前补跑独立容器完整后端单测：`docker compose -p doc-parser-check -f "$CHECK_COMPOSE" exec -T -w /workspace/backend -e PYTHONPATH=/workspace/backend/package:/workspace/backend -e API_KEY_DERIVATION_SECRET=doc-parser-check-temporary-secret-at-least-32 api uv run --no-sync --group test pytest test/unit -m "not slow" -q`，2348 passed。`CHECK_COMPOSE` 指向仅用于本次验证的 Compose 文件，容器将当前仓库挂载为 `/workspace`；`--no-sync` 复用按锁文件构建的镜像依赖。

HTTP 回归固化在 `backend/test/integration/api/test_legacy_doc.py`，标准入口为 `docker compose exec -T api uv run --group test pytest test/integration/api/test_legacy_doc.py -q`，复用现有真实 HTTP 登录 fixture。此次隔离验证将同一测试及固定 DOC 样本复制到 `/check`，执行 `docker compose -p doc-parser-check -f "$CHECK_COMPOSE" exec -T -e PYTHONPATH=/check:/app/package:/app api python -m pytest -c /app/pyproject.toml --confcutdir=/check/integration/api -p doc_http_fixtures /check/integration/api/test_legacy_doc.py -q`：1 passed。`doc_http_fixtures` 只把客户端地址设为临时 Uvicorn，并提供空认证头；服务端身份依赖使用上述固定身份，其余路由、存储、DOC 转换与清理均执行真实实现。
