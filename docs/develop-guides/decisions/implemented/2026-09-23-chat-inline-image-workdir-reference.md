# 聊天内联多图落盘为 Workdir 参考图

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/attachment_service.py

## 问题

聊天图片已经持久化在消息的 content_blocks 中，但需要本地文件的图片工具无法使用它们。只描述图片而没有参考文件路径，不能完成参考图输入。

## 决策

在 worker 持有的 PreparedRunExecution 中，chat_service 遍历本轮持久化 HumanMessage 的全部图片块。通过当前用户与 context.workdir_relative_path 打开既有 Workdir，调用 persist_inline_chat_image，经 no-follow 文件边界写入 uploads，再将返回记录加入本轮 attachment_context。

首张文件名为 `chat-image-<request_id>.<ext>`，后续为 `chat-image-<request_id>-<序号>.<ext>`。重试覆盖同一文件，不生成随机副本。文件内容为持久化消息里的图片字节；格式按魔数识别 PNG/JPEG/GIF/WEBP/BMP。

记录不写入会话附件元数据。解码失败或格式未知返回 None；写盘失败记 warning，不阻断原始多模态输入。路径来自 Workdir 映射，不混用宿主和 Sandbox 路径。模型是否需要转述由后续[输入模态声明决定](2026-09-24-model-input-modalities-declaration.md)。

## 替代方案

- 改为前端添加附件：改变输入协议与会话附件生命周期，不适合本消息自带图片。
- 只写盘不注入路径：模型无法确定文件与图片的对应关系。
- 随机文件名：重试会产生重复文件。

## 后果

每张图保留一个工作区文件，仅本轮注入参考路径。会话附件删除不负责清理这些文件。普通文件路径及工具参数不做模糊纠正；文件边界负责报告不存在或非法路径。

## 验证

- attachment_service 单测回读写盘字节，验证重试稳定、非法 base64 与未知格式不写文件。
- chat_attachment_context 单测验证路径进入模型文本，无记录时不生成上下文。
- bash backend/test/e2e/runs/run_inline_images.sh API_IMAGE PROVISIONER_IMAGE：一次性空库、真实 HTTP/API/worker/PostgreSQL，逐一回读两张有效 PNG 的完整字节、Run 输出归属、completed 事件及模型 wire 中的两条文件路径；2026-10-13 本地 1 passed，已加入 Runtime E2E workflow。

未验证：真实付费图片生成供应商利用文件生成图片；生产部署；浏览器上传处理过程。
