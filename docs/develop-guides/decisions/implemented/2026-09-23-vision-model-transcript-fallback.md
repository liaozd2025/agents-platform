# 不支持图片的主模型通过视觉模型转述继续工作

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/image_input_service.py

## 问题

不支持视觉输入的模型拒绝图片，整轮对话无法继续。部分兼容供应商只返回 Unexpected item type in content，不含 image 关键词。2026-09-23 作者直连 DashScope 的历史实验表明 qwen3.7-max 拒图，而 plus/flash 可以处理；该结论需要随供应商升级重新复核。

## 决策

管理员配置 vision_model，默认 alibaba-cn:qwen3.7-flash。异步中间件仅在本轮存在图片且错误明确表示拒绝图片时转述，再调用主模型。已声明能力的预路由由后续[输入模态声明决定](2026-09-24-model-input-modalities-declaration.md)补充。

转述保留原始文字与参考路径，把图片块替换为带 source 的 image_transcript 文本，说明其为二手转述。主模型身份优先取 metadata.yuxi_model_spec，未标注时采用模型名；视觉模型与主模型相同则跳过。

服务入口从运行 context、中间件入口从 runtime.context 传递 uid 与 thread_id，视觉模型工厂用同一用户签名与会话路由契约。转述缓存键包含 UID、线程、视觉模型、图片及问题指纹；TTL 600 秒，每次访问清理过期项，容量最多 256 项。缓存仅减少同用户同线程工具循环的重复调用。

只有未声明能力的模型可学习拒图记录；明确声明始终优先。拒图记录同样回收过期项并限制容量。转述不可用时保留 OCR fallback，且仅当智能体实际启用 ocr_parse_file 才允许工具调用。同步入口只使用既有 OCR fallback。转述成功后的主模型重试异常原样传播，不以 OCR 响应掩盖认证、限流或其他失败。

## 替代方案

- 仅识别拒图错误：能提供说明，但不能完成图片理解。
- 让用户自行更换模型：无法解决默认模型下的图片任务。
- 让模型主动调用转述工具：多一轮决策且无法保证调用。
- 跨用户共享转述缓存：会跳过不同身份供应商请求，违反调用归属。

## 后果

图片输入可能增加一次视觉请求，结果属于二手证据。视觉配置错误或供应商不可用时可观察降级。长驻 worker 不永久保留过期转述。普通文件和 read_file 参数不自动改写，避免静默读错另一文件。

## 验证

- middleware 单测覆盖拒图识别、未配置及失败的转述、视觉模型等于主模型、OCR 禁用、无关供应商错误、重试 401/429 原样传播和 runtime 身份传递。
- image_input_service 单测覆盖原消息不变、转述来源、身份与线程隔离、过期清理和容量上限。
- run_inline_images.sh 本地 1 passed：合成 OpenAI 服务严格验证真实出站 UID/HMAC/时间戳，缺失签名返回 401；未知模型先拒图，再收到文本转述完成运行。真实 API/worker/PostgreSQL 和文件均参与，非外部模型质量测试。

未验证：当前真实视觉供应商的识别质量、收费与可用性；生产部署；同步视觉转述（不提供）。
