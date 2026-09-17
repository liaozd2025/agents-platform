# 消息附件可直接预览，加密或损坏的 Office 文件在预览管线快速失败

状态：implemented
类型：feature
Owner：backend/package/yuxi/utils/filepreview.py

## 问题

两个相关问题都出在"文件预览"这条链路上，且互为前后：

**一、消息里的文件附件没有查看入口。** 用户发送文件后，消息气泡里的附件卡片只渲染图标、文件名与「类型 · 大小」，没有任何交互；用户上传的图片走的是另一条分支（`message.image_content` 内联 `<img>` 加点击全屏大图），所以只有图片能看。右侧面板与后端其实早已支持 doc/docx/ppt/pptx 预览（`workspace/preview.py` 判定 office 文件后由 LibreOffice 转 PDF），缺的只是把气泡卡片接到这条链路上。

**二、疑似加密的 Office 文件会把用户拖进 60 秒白等，并只给一句无指向性的报错。** 用户上传的一张 641 KB 的 pptx 点击预览时报 `{"detail":"Office 文件转换 PDF 超时（60 秒）"}`，界面上只显示「请求参数错误」。取证结论是文件本身在企业加密（DLP）保护下：该文件在 Windows 桌面上的头字节是 `63 c0 b6 4d 0d 50 c1 2f`（zip 的 `PK\x03\x04` 与 OLE2 的 `d0cf11e0a1b11ae1` 两个签名都不成立），上传到服务端 workdir 后的字节与磁盘**完全一致**（同为 656936 B）。LibreOffice 无法识别这类内容，没有报错也没有快速失败，降级成 `as a Writer document` 硬转，输出 31 MB 垃圾 PDF、耗时 **74 秒**，越过 `OFFICE_PREVIEW_TIMEOUT_SECONDS`（默认 60）后超时。同一容器、同一参数转换同批上传的明文 docx 只需要 **1 秒**。也就是说：耗时的成因是非法输入触发了降级转换路径，与机器性能无关；用户既拿不到文件，也拿不到「文件为什么打不开」的原因。

## 决策

本决定跨越两个事实 Owner：「加密/损坏输入不得进入转换器，并返回可读结论」由 `backend/package/yuxi/utils/filepreview.py` 与 `backend/package/yuxi/workspace/preview.py` 拥有；「消息附件可点击并在右侧面板打开」由 `web/src/utils/file_utils.js`、`web/src/components/AgentMessageComponent.vue`、`web/src/components/AgentChatComponent.vue` 拥有。两者通过「预览接口返回结构化 unsupported payload」这一响应契约衔接，卡片本身不判断文件是否加密。

前端负责把点击接到既有预览链路上，后端负责在预览入口识别"不可能被解析的输入"并给出可读结论。

前端：

- `web/src/utils/file_utils.js` 的 `normalizeAttachmentPreview` 输出新增 `path`，取值为 `original_path || path`，即 Sandbox runtime 的绝对虚拟路径（原文件优先，附件被解析过时回退到解析产物）。旧数据没有路径时留空。
- `web/src/components/AgentMessageComponent.vue` 把附件卡片从 `<div>` 改为 `<button type="button">`：有 `path` 时可点击并 `emit('open-attachment', { path, name })`，无 `path` 时 `disabled`，外观与改动前保持一致（补 `font: inherit`、`text-align: left`，新增 `is-previewable` 的 hover/focus 样式）。
- `web/src/components/AgentChatComponent.vue` 在消息组件上接住事件，复用既有 `openArtifactPreview` 打开右侧面板；面板标签名沿用 `getPanelFileName` 的「`file.name` 优先」规则，因此显示的是原始文件名而不是 `fid_文件名` 这种存储名。子智能体只读视图（`ThreadMessageList.vue`、`ConversationProcessGroupComponent.vue`）本次不接。

后端：

- `backend/package/yuxi/utils/filepreview.py` 新增 `office_container_signature_mismatch()`：对 Office 后缀（doc/docx/ppt/pptx/xls/xlsx）要求文件是 zip（`PK\x03\x04`、`PK\x05\x06`、`PK\x07\x08`）或 OLE2（`d0cf11e0a1b11ae1`）容器之一，两者都不成立才判定为加密或损坏。**刻意放行 OLE2**：改名文件（`.docx` 后缀装旧格式内容）与带密码的 OOXML 都是 OLE2 容器，需要继续交给 LibreOffice 按内容判断，避免制造新的误报。
- `backend/package/yuxi/workspace/preview.py` 的 `preview_workspace_file` 在入口处做该判定，命中时记一条 warning 日志并返回 `PreviewResult(content=None, preview_type="unsupported", supported=False, message="无法预览：文件内容不是有效的 Office 文档，很可能已被加密（如企业加密软件）或已损坏。请在本地用 Office 打开确认后，上传未加密的副本。")`，**不抛 400**。这一点是本决策的关键取舍：前端 `web/src/apis/base.js` 会把所有非 422 响应的 `detail` 统一替换成通用文案，抛错等于把「可能已加密」这条关键信息换成「请求参数错误」；返回结构化 payload 则让前端走既有的不支持预览分支（与 `preview_too_large()` 同一约定）把后端原文直接展示给用户。

## 替代方案

- **让前端透传所有 400 的 detail**：`base.js` 只对 422 保留结构化 detail 是有意为之（错误原文可能携带服务端内部信息），为一处提示放开全局行为不划算，未采纳。
- **调大 `OFFICE_PREVIEW_TIMEOUT_SECONDS`**：74 秒只是降级转换的耗时，产出的 31 MB 垃圾 PDF 还会被写进 `runtime/cache/office-previews` 缓存，治不了本。
- **只按 OOXML 严格校验（docx/pptx/xlsx 必须是 zip）**：会把改名文件与带密码容器一并拦掉，产生新的误报，未采纳。
- **在服务端解密 DLP 密文**：解密依赖 Windows 侧授权进程，Linux 运行时没有这个能力，方案不成立。
- **点击卡片改为浏览器下载**：下载得到的仍是密文，对用户无价值，未采纳。

## 后果

- 疑似加密或损坏的 Office 文件预览从「等约 60 秒后得到一句通用错误」变为毫秒级返回明确提示，且不再把 LibreOffice 的降级输出写入预览缓存；真实损坏（非加密）的文件得到同一提示，文案已包含「或已损坏」。
- 带密码的 OOXML（OLE2 容器）仍会进入 LibreOffice，其失败信息目前依赖 soffice 的输出，本次未单独处理。
- 消息附件的可点击性依赖后端返回的 `original_path` / `path` 字段；缺失该字段的历史数据保持不可点击，行为与改动前一致。
- 加密文件本身依然无法预览：要看到内容必须先取得解密后的明文副本再上传，本变更只负责把原因讲清楚。
- 前端交互只覆盖主对话区，子智能体线程与过程折叠区仍没有点击入口。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 消息里的文件附件点击后在右侧面板预览 | 卡片无点击绑定、事件未冒泡、面板拒绝加载绝对虚拟路径 | `web/src/components/AgentMessageComponent.vue`、`web/src/components/AgentChatComponent.vue` | 隔离渲染页（临时 vite config 只挂载消息组件）经 playwright 断言：三张卡片 `tagName` 均为 `BUTTON`，有 `path`/仅有 `original_path` 的可点、缺路径的 `disabled`，点击载荷为 `{path,name}`，页面零错误；`node --test test/unit/messageAttachmentPreview.test.js` 3 项（装配链守卫） | 无路径（旧数据）附件不产生事件、不打开面板 | Passed |
| 前端能显示"文件可能已加密"的提示 | 后端 400 的 detail 被 `base.js` 替换成「请求参数错误」，提示丢失 | `backend/package/yuxi/workspace/preview.py` | 隔离渲染页喂入后端同结构的 JSON payload，面板文本与状态实测为 `unsupported` / `supported=false` 且包含「加密」；截图 `docs/vibe/attach-preview/encrypted-preview-hint.png` | 同一 payload 若改成 `preview_type: pdf` 不会进入该分支（隔离页只验证命中分支） | Passed |
| 加密/损坏的 Office 文件不触发 LibreOffice | 转换器被调用后白等 60 秒并产出垃圾缓存 | `backend/package/yuxi/utils/filepreview.py` | 容器内用**真实密文 pptx** 调用 `preview_workspace_file`：**0.001 秒**返回 `supported=False`、`preview_type=unsupported`、提示含「加密」；`test/unit/workspace/test_preview.py` 用记录器替换 `convert_office_to_pdf` 并断言零调用（密文 pptx、密文 xlsx 各一条） | 负向守卫本身即失败面：密文若被放行则 `convert_office_to_pdf` 被调用，测试立即失败 | Passed |
| 正常的 Office 文件仍照常转 PDF 预览 | 守卫过宽把合法文件一并拦下 | `backend/package/yuxi/utils/filepreview.py` | 容器内真机：明文 docx 签名判定为不匹配=False、转换耗时 1 秒、输出 362 KB 正常 PDF；`test/unit/workspace/test_preview.py` 断言合法 OOXML 走转换路径 | `test/unit/utils/test_filepreview.py` 参数化覆盖 zip/OLE2 两种合法容器（含改名文件与带密码容器）必须放行 | Passed |
| 预览状态不影响既有的表格与文本预览 | 守卫放在函数入口，可能误伤非 Office 后缀 | `backend/package/yuxi/workspace/preview.py` | 容器内 `pytest test/unit/utils test/unit/workspace test/unit/services/test_artifact_service.py test/unit/services/test_workspace_service.py -q` → 114 passed；`pytest test/unit/services -k "viewer or artifact or workspace"` → 92 passed（覆盖 `preview_workspace_file` 的全部三个调用方） | 非 Office 后缀直接返回 False，参数化用例覆盖 `.md`、`.pdf`、`.txt` | Passed |
| 前端改动不破坏既有构建与规范 | 模板语法或 lint 违规 | `web/src/components/AgentMessageComponent.vue` | `eslint`（改动 3 个源文件 + 新增测试）无输出；`vite build --outDir $TEMP/...` 构建成功 | 既有 `test/unit/agentPanelSections.test.js` 用例 8 为工作区存量红灯，该断言只读本次未改动的 `AgentPanel.vue` | Passed |

未执行项：后端 `test/integration/api/*` 与 E2E 套件在本机无法运行（本地 compose 缺少 `sandbox-provisioner`，session 级 fixture 会在收尾阶段挂死），CI 对 assembled path 亦无覆盖，因此「浏览器真实会话中点开附件并加载出 PDF」只做了逐段证据（后端函数级 + 前端组件级），没有一条从上传到面板渲染的端到端记录，记为 `Not run`。全量 `pytest test/unit` 单次运行超过 15 分钟未结束，未取到结果；改动只落在预览管线，已按调用方做定向覆盖。
