# 不支持图片的主模型通过视觉模型转述继续工作

状态：implemented
类型：feature
Owner：backend/package/yuxi/agents/middlewares/model_input.py

## 问题

主模型不支持图片输入时，带图对话直接整轮失败。实测 DashScope：`qwen3.7-max` 对任何 `image_url`（对象形式、字符串形式、纯图片、图文混排）都返回 400 `Unexpected item type in content.`，而同一张图在 `qwen3.7-plus` / `qwen3.7-flash` 上正常并被正确识别；纯文本与纯 text 数组在 max 上都是 200，因此不是 content 格式问题，是模型本身没有视觉能力。平台的默认对话模型正是 `alibaba-cn:qwen3.7-max`，所以不显式选模型的用户一贴图就踩到。

既有兜底链有两个缺口：

- `_is_image_input_rejection()` 要求文案同时含 image/vision 关键词与拒绝词，DashScope 的 `Unexpected item type in content.` 一个都不命中，连兜底入口都进不去。
- OCR 兜底（`ocr_parse_file`）要求图片在沙盒里有路径，而 `_read_file_image_paths()` 只从 `read_file` 的 ToolMessage 取路径；聊天输入框贴的图走 base64 多模态通道，没有路径，即使前提成立也只能落到"没有可供 OCR 工具解析的文件路径"。

平台也没有可用的能力声明：`input_modalities` 字段与校验都在（`models/providers/cache.py`、`models/providers/service.py`），前端 `utils/modelMetadata.js` 据此算 `vision` 标志，但 DashScope 远端 `/models` 不返回能力元数据、本地 `enabled_models[].extra` 为空，实测 `/api/system/model-providers/models/v2` 对 alibaba-cn 完全不返回 `input_modalities`，因此无法在选择模型或提交请求时提前拦截。

## 决策

在图片兼容层补一层「视觉转述」，语义对齐 WorkBuddy 模型配置里的 `relatedModels.vision`（其文档标注该 variant 为"预留未启用"，故只借骨架）：

- `system_options` 新增 `vision_model`（`type: model`，默认 `alibaba-cn:qwen3.7-flash`），与既有 `fast_model`/`default_ocr_engine` 同一套配置写法，支持管理员与 `environment` 覆盖。
- `ImageInputCompatibilityMiddleware.awrap_model_call()` 在「本轮消息带图片」且「主模型拒绝了图片」时，用配置的视觉模型对同一张图与同一段用户问题调用一次，把消息里的图片块换成 `<image_transcript source="<spec>">` 包裹的文本（明确标注是转述、不是原图），保留原文与其中的图片路径线索，然后重试主模型。
- 主模型不可用或转述失败时按序降级：转述不可用 → 既有 OCR 兜底 → 仍失败才抛原始错误。视觉模型与主模型相同时不再调用（必然同样被拒）。
- 进程内两层缓存，TTL 600 秒：按 `yuxi_model_spec` 记住"该模型不接受图片"，后续请求直接转述、不再发一次注定失败的调用；按 (视觉模型, 图片指纹, 问题指纹) 缓存转述，多步工具循环里同一张图只向视觉模型付一次费。
- 模型身份取自 `model.metadata["yuxi_model_spec"]`（`models/chat.py` 加载时写入），未标注元数据的模型视为未知（不参与"与主模型相同"判断）。

同步路径 `wrap_model_call()` 保持原样（只走 OCR 兜底）：同步分支无法 await，且 worker 实际走异步路径。

## 替代方案

- 只扩错误文案识别、不加转述：能避免整轮 400，但用户只会得到"当前模型无法读取图片，且没有可供 OCR 工具解析的文件路径。"（负向验证实测正是这句），图片理解仍然缺失。
- 要求用户改用支持图片的模型：零代码，但不解决默认模型贴图必失败的默认路径。
- 把视觉模型调用提前到服务层提交请求时（真正的预路由）：需要可信的 `input_modalities` 声明，而该数据源目前为空（DashScope 不提供、本地未手工声明）。这是后续工作，落地前先用错误驱动的兜底保证不炸。
- 把转述做成让模型自己调用的工具：多一轮工具调用且依赖模型自觉，不如兜底确定。

## 后果

- 不支持图片的主模型上，每张图会多一次视觉模型调用；命中转述缓存后同一张图同一问题不再重复调用。转述是二手信息，注入文本已显式标注来源，避免主模型当成一手证据。
- 放宽 `_is_image_input_rejection()` 后，带图请求里 `Unexpected item type in content.` 这类"只说 content 项非法"的 400 会被归因为图片问题。该判定始终以"本轮消息确实带图片"为前提，无关错误仍原样抛出（`test_does_not_mask_unrelated_provider_errors_when_image_is_present`、`test_does_not_report_malformed_image_as_unsupported_model` 覆盖）。
- `vision_model` 配成一个没有视觉能力的模型时不会死循环，只会退化为 OCR 兜底路径（有单测覆盖）。
- 仍未解决的是"选择模型时就知道能不能贴图"：那需要把 `input_modalities` 变成可信、可编辑的数据并接到 UI 与提交校验上。

## 验证

| 主张 | 语义 Owner | 直接证据 | 负向案例 | 当前结果 |
|---|---|---|---|---|
| 默认模型（qwen3.7-max）贴图后能基于图片内容作答 | `yuxi/agents/middlewares/model_input.py` | 真实 HTTP：`POST /api/chat/thread` + `POST /api/agent/runs`（不传 `model_spec`，带纯红方块 PNG base64），run `b57511ea` `completed`；worker 日志同时出现 `model='alibaba-cn:qwen3.7-max'` 与 `图片已由视觉模型 alibaba-cn:qwen3.7-flash 转述，字符数=81`；助手回复「红色」 | 临时让 `_transcribe_images()` 直接返回 `None` 后同一脚本：run `78a55da2` 仍 completed，但助手回复变成「当前模型无法读取图片，且没有可供 OCR 工具解析的文件路径。」（图片理解缺失的原状） | 通过 |
| 主模型第二次调用不再含图片块，改为转述文本 | 同上 | `test_uses_configured_vision_model_when_provider_rejects_image`（断言第二次消息 content 块为 `["text","text"]`、含 `<image_transcript source="...">` 与转述内容、原文保留；转述请求 content 为 `["text","image","text"]`） | 去掉转述重试后该断言直接失败（handler 只被调用一次、图片仍在） | 通过 |
| 已判定不支持图片的模型后续请求不再空跑一次失败调用，且转述只付一次费 | 同上 | `test_known_non_image_model_transcribes_before_calling_handler`（两次 `awrap_model_call`：handler 共 3 次 = 1 失败 + 2 成功，视觉模型只被调用 1 次） | 去掉 spec 缓存则 handler 会变成 4 次、去掉转述缓存则视觉模型会变成 2 次，两个断言分别失败 | 通过 |
| 未配置视觉模型、视觉模型调用失败、视觉模型与主模型相同时均不阻断本轮，退回 OCR 兜底 | 同上 | `test_falls_back_to_ocr_when_vision_model_is_not_configured`、`test_falls_back_to_ocr_when_vision_model_call_fails`、`test_keeps_image_when_vision_model_is_the_main_model` | 三个用例的 `load_chat_model` 替身会在不该调用时抛 `AssertionError`，一旦多调即失败 | 通过 |
| 无关 400 与畸形图片报错不被误判为"模型不支持图片" | 同上 | 既有 `test_does_not_mask_unrelated_provider_errors_when_image_is_present`、`test_does_not_report_malformed_image_as_unsupported_model` 保持通过 | 若把判定放宽成"带图片即兜底"，这两条会由抛错变为返回兜底响应 | 通过 |
| 无回归 | `backend/test/unit/` | `docker exec test-api-1 sh -c "cd /app && python -m pytest test/unit -m 'not slow' -q"` → 2299 passed, 53 skipped | 无 | 通过 |

未覆盖：视觉转述在多模态主模型上不会触发（只在被拒时进入）；转述质量对同一张图的稳定性依赖视觉模型能力，未做逐图人工比对。
