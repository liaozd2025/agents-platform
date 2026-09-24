# 聊天输入的内联图片落盘为 Workdir 参考图

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/attachment_service.py

## 问题

聊天输入框里贴/拖的图片走的是 base64 多模态通道：前端 `web/src/utils/multimodal_image_upload.js` 调多模态上传接口拿到 `image_content`，`input_message_service.build_chat_input_message()` 把它组成 `image_url` data URL 放进 `HumanMessage`，并随 `AgentRunRequest` 落到消息表。整个过程没有任何写盘动作，图片只存在于消息里。

因此需要真实文件路径的工具拿不到这张图。内置 `image-gen` 技能的参考图必须先上传本地文件（`image_gen.py upload --file /home/gem/user-data/...`，SKILL.md 第 23~27 行），只有「添加附件」通道（`attachment_service._store_attachment` → Workdir `/uploads/<file_id>_<name>`）才会产生它需要的路径。

现场证据（本地服务，会话 `生成小红书种草图`，thread `0a821220-7007-4816-a26f-cdead5f9017f`，workdir `projects/2026-09-23_16-04-18_4abab451`）：

- `messages` id=265：`message_type=multimodal_image`、`image_content` 为 base64 PNG、`extra_metadata.attachments` 为空，所以本轮模型输入里没有 `<attachment_context>`。
- 工具审计：`ls .../uploads` 返回 `path_not_found`，`glob **/*.{png,jpg,jpeg,webp}` 返回 `No files found`；宿主 workdir 下只有 `outputs/`。
- 模型先准确描述了图片（"Schick 男士手动剃须刀"），随后自述"项目目录中没有找到上传的文件"，退化为纯文生图。

## 决策

在会话运行的服务层补齐这条链路，前端与请求协议不变。

`attachment_service.persist_inline_chat_image()` 负责写盘与路径映射：按魔数识别 PNG/JPEG/GIF/WEBP/BMP，经与对话附件同一条 no-follow 通道（`Workdir.copy_file_from_path`）写入 `<workdir>/uploads/chat-image-<request_id>.<ext>`，返回与对话附件同形的记录（`file_id/file_name/file_type/path/...`），`path` 是该文件在 Sandbox 内的绝对路径。

`chat_service.stream_agent_chat()` 负责模型可见输入：本轮输入带 `image_content` 时调用它，把返回的记录追加到传给 `_with_attachment_context` 的附件列表，于是本轮模型输入里出现指向这张图的 `<attachment_context>` 行。文件名校验沿用 `_safe_file_name`，以 `request_id` 为种子，同一请求重试时覆盖同一文件而不产生副本。

这类图片不写入 Conversation 附件元数据：不出现在会话附件列表，不受附件删除接口影响，只作为本轮运行的文件落点。内容无法解码或格式无法识别时返回 `None`，不写文件也不注入路径；写盘异常按本轮无路径降级并记 warning，不阻断对话。

## 替代方案

- 前端把输入框图片改走「添加附件」通道（tmp 上传 + confirm）。后端零改动，但要重写输入框上传流程，且图片会长期挂在会话附件列表里，与"本消息自带的图"语义不符；同时受 5 MB 附件上限约束。
- 只落盘、不注入 `attachment_context`。模型仍不知道该文件与消息里的图是同一张，只能靠 `glob` 猜，等于把问题留给模型。
- 用时间戳命名文件。重试或 `agent_run_attempts` 重放会反复产生副本，故改用 `request_id` 命名。
- 落盘后顺手把记录写进会话附件元数据。会让内联图片在后续每轮都被重复注入，且用户无法区分它与主动添加的附件。

## 后果

- 每条带图消息会在 `uploads/` 留下一个 `chat-image-*.png` 文件。它不是会话附件，附件删除接口不覆盖它，需要时按工作区文件处理。
- 只有带图的那一轮注入路径；后续轮次依赖模型自行 `ls`/`glob` 发现工作区里的文件。文件名前缀 `chat-image-` 提供了可辨认的线索。
- 写入的是前端已处理过的图片（`image_processor`：≤5 MB、已按 EXIF 纠正方向、JPEG/PNG 重编码），不是用户原始字节。
- 默认模型 `alibaba-cn:qwen3.7-max` 不接受图片输入（DashScope 直接返回 400 `Unexpected item type in content.`），无论 2 段还是 3 段 content 都失败；该文案不含 `ImageInputCompatibilityMiddleware` 的 image/vision 关键词，因此不会触发 OCR 兜底。这是与本次改动无关的既有行为，用户需自行选择支持视觉的模型（如 `qwen3.7-flash`）。

## 验证

| 主张 | 语义 Owner | 直接证据 | 负向案例 | 当前结果 |
|---|---|---|---|---|
| 带图消息的参考图落到 Workdir，且路径进入本轮模型输入 | `yuxi/services/attachment_service.py`、`yuxi/services/chat_service.py` | 真实 HTTP：`POST /api/chat/thread` + `POST /api/agent/runs`（带 160×160 PNG base64，`model_spec=alibaba-cn:qwen3.7-flash`）→ run `e1a6b66a` `completed`；宿主出现 `projects/2026-09-23_16-31-06_d1e5070d/uploads/chat-image-8d5d1f9c-14e7-406f-a586-e9c857563c47.png`（449 B，与入参一致）；`checkpoint_blobs` 的 `messages` 通道命中 `chat-image-`；助手回复复述的 `<attachment_context>` 含该 runtime 路径 | 把 `if image_content:` 临时改为恒假后同一脚本：run `b8654b8e` 仍 `completed`，但 `checkpoint_blobs` 命中 0、workdir 无 `uploads/`、模型回复"没有收到任何 `<attachment_context>` 标签内的文字"（原缺陷复现） | 通过 |
| 解码失败或格式无法识别时不写出伪图片文件、不注入路径 | `yuxi/services/attachment_service.py` | `test_persist_inline_chat_image_rejects_content_it_cannot_verify`（非法 base64 与纯文本 payload 均返回 `None`，`workdir` 无任何写入） | 该用例本身就是"写入未知字节"这一错误实现的反例 | 通过 |
| 同一请求重试覆盖同一文件，不产生重复副本 | 同上 | `test_persist_inline_chat_image_writes_retry_stable_reference_file`（同 `request_id` 两次调用后仅 1 个文件，路径一致） | 若改用随机名，断言 `len(files) == 1` 即失败 | 通过 |
| 落盘记录能被附件上下文透传为模型可见文本 | `yuxi/services/chat_service.py` | `test_inline_chat_image_path_reaches_model_input`（真实调用 `persist_inline_chat_image` + `_with_attachment_context`，断言路径与文件名出现在 `content` 中） | `test_model_input_has_no_reference_path_without_inline_image_record`（无记录时不得出现 `<attachment_context>`） | 通过 |
| 既有单元测试无回归 | `backend/test/unit/` | `docker exec test-api-1 sh -c "cd /app && python -m pytest test/unit/services test/unit/agents -q"` → 972 passed | 无 | 通过 |

未覆盖：SubAgent 运行（`run_type=subagent`）不携带内联图片，走不到该分支；沙盒内工具实际用该参考图完成图生图未在本记录内验证（Kie 调用需真实密钥与付费任务）。
