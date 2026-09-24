# 模型图片能力声明驱动运行时图片预路由

状态：implemented
类型：feature
Owner：backend/package/yuxi/models/providers/service.py

## 问题

[视觉模型转述兜底](./2026-09-23-vision-model-transcript-fallback.md) 让不支持图片的主模型也能处理带图对话，但判定方式是"先发一次请求、被拒后再转述"：

- 每个进程内首次遇到某个不接图的模型，都会先发一次注定失败的请求（DashScope 对 `qwen3.7-max` + 图片固定返回 400）。
- 这套"学习"会误伤：一个其实支持图片的模型只要返回了 content 类型错误，就会被记成"不支持图片"10 分钟，期间带图请求都先转述、原生视觉被跳过。
- 平台已有 `input_modalities` 字段与校验，但数据源是空的：`_normalize_remote_model()` 从远端 `/models` 的 `architecture.input_modalities` 取值，而 DashScope 的 `/models` 只返回 `id/object/created/owned_by`。声明为空时无法预判。

## 决策

把 `input_modalities` 作为运行时唯一的能力判定依据，但**只在服务端消费，不对外暴露**。

- **声明落库**：内置 provider 模板为已知模型声明图片能力——`alibaba-cn` 的 `qwen3.7-max = ["text"]`、`qwen3.7-plus` / `qwen3.7-flash = ["text", "image"]`（均为直连 DashScope 的实测结论：max 对任何 `image_url` 都返回 400 `Unexpected item type in content.`，plus/flash 能正确识图）。
- **只补空值**：`ensure_builtin_model_providers_in_db()` 对已存在的内置 provider 逐模型比对，只在模型缺声明时补上，不覆盖管理员在模型管理页填写的值；无改动时不写库。
- **运行时预路由**：`chat_service.stream_agent_chat()` 在本轮带图、且所选模型被声明为"确定不含 image"时，先用 `system_options.vision_model` 转述成文本再交给主模型，主模型不产生失败请求。判定与转述都在 `services/image_input_service.py`，与图片兼容中间件共用同一实现。
- **语义**：声明非空且不含 `image` = 确定不支持（预路由）；声明为空 = 未知能力（仍走中间件的错误驱动兜底），因此未声明的新模型不会因为"没填"被无谓转述。
- **不对外暴露**：`GET /api/system/model-providers/models/v2` 不返回 `input_modalities`；模型选择器上的"支持图像输入"标记继续由前端静态快照 `@opencode-ai/models/snapshot` 提供，其 `alibaba-cn` 条目与本次实测一致（`qwen3.7-max` 只含 text，plus/flash 含 image）。

## 替代方案

- 把声明同时透出到 `/models/v2` 并让选择器优先用它：初版做法，已撤回。理由是快照已经能给出同样的界面标记（实测确认），再暴露一份平台自己的声明只会让界面能力标记出现两个可能互相矛盾的来源，而界面并不需要由运行时数据驱动。
- 只做前端标记、不做预路由：声明与运行行为脱节，等于把已经算出来的结论浪费掉。
- 用"首次调用探测"自动得出能力：探测本身就是一次注定失败的请求，且 DashScope 的报错文案不含 image 关键词，结论不可靠；还会与管理员手工声明形成两个事实来源。

## 后果

- 模型管理页的「输入类型」字段进入运行时链路：改它会改变带图请求是否先转述。界面上没有额外提示，管理员需要知道这个字段的实际作用。
- 内置同步在每次 API 启动时把缺失声明补回：在管理页清空某个内置模型的值后重启会恢复为内置声明。
- 能力判定读取 `model_cache`（Redis + 5 秒进程内缓存），管理员改动最多 5 秒后跨进程生效。
- **路径抄写纠错**：长随机文件名是模型抄写易错点，实测 max 把 `9fc2ac55…_preview.png` 抄成 `9fc2c554…` 导致 read_file 404、整轮道歉。两层纠正：① 转述生成后，把转述文本里的路径与消息原文中的真实路径比对，同目录、相似度 ≥0.9 且无第二个同样接近的候选时自动纠正；② `read_file` 工具执行前，对与上下文真实路径高度相似且无歧义的 `file_path` 做同样纠正。纠正只发生在同父目录内，完全匹配或有歧义（第二接近候选差距 <0.03）时不动，避免改错模型本意。
- 转述调用附带 `max_tokens=1000` 与 `enable_thinking=false`（仅转述这一处，不影响正常对话）。实测 qwen3.7-flash 对 ~1MB 图片：默认思考且不限长为 53.0s / 2657 completion tokens，两者都加为 1.5~3s，转述质量无可感知差异。调优参数只发给 OpenAI 兼容系（`provider_type == "openai"`，实测 Anthropic/Gemini 客户端会把 `extra_body` 转成请求里的未知字段被供应商拒绝）；OpenAI 兼容系里仍可能被拒绝（如官方 API），请求失败后退回一次朴素调用并记住该 spec（`_params_rejected_specs`），网络类失败也会触发这次额外重试，代价可接受。
- 声明是人工维护的事实：新接入模型默认"未知"，带图请求仍会先失败一次再兜底（可接受，日志会记录）。DashScope 三个模型的结论来自 2026-09-23 的直连实测，供应商升级模型后需要复核。

## 验证

| 主张 | 语义 Owner | 直接证据 | 负向案例 | 当前结果 |
|---|---|---|---|---|
| 内置声明落库，且只补空值、不覆盖管理员配置 | `yuxi/models/providers/service.py` | `test_builtin_provider_declares_input_modalities_for_dashscope_chat_models`、`test_merge_builtin_model_modalities_only_fills_missing_values`（已有 `["text","audio"]` 保持、缺失项补齐、未登记模型不动、二次调用返回 False）；重启 api 后 `model_providers.enabled_models` 与 `model_cache` 都带上声明 | 手工清空 max 的声明并 `models/cache/refresh` 后缓存返回 `[]`，再重启 api 又被内置模板补回（"只补空值"生效）；若改成无条件覆盖，"保留 `["text","audio"]`"的断言即失败 | 通过 |
| 声明为"不支持图片"的模型带图时走预路由，主模型不产生失败请求 | `yuxi/services/chat_service.py` | 默认模型（`alibaba-cn:qwen3.7-max`）+ 纯红方块 PNG 的真实请求：worker 日志「模型 alibaba-cn:qwen3.7-max 已声明不支持图片，先把图片转述为文本再交给主模型」+「图片已由视觉模型 alibaba-cn:qwen3.7-flash 转述」，run `completed`、助手回答「红色」 | 把 max 的声明从内置模板与数据库中一并移除并刷新缓存后重跑：日志变成中间件的「模型 alibaba-cn:qwen3.7-max 拒绝了图片输入，改为视觉模型转述后重试」——主模型确实被调用并失败了一次，随后兜底仍得到「红色」 | 通过 |
| 声明语义：只有"非空且不含 image"才预判，空声明视为未知 | `yuxi/services/image_input_service.py` | `test_declared_text_only_requires_a_non_empty_declaration`（`["text"]`→True；`["text","image"]` / `[]` / 未登记 / None→False） | 若把空声明也判为"不支持"，未知能力的模型会被强行转述、原生视觉被跳过，该用例失败 | 通过 |
| 转述守卫（未配置视觉模型 / 视觉模型等于主模型 / 无图片）不调用模型，且不就地修改原消息 | 同上 | `test_transcribe_skips_when_vision_model_is_not_configured`、`test_transcribe_skips_when_vision_model_is_the_main_model`、`test_transcribe_replaces_image_blocks_and_keeps_text` | 替身在"不该调用"时抛 `AssertionError`，多调即失败 | 通过 |
| 转述延迟受控（关思考 + 限长），回答质量不回退 | 同上 | DashScope 直连对照（~1MB 图片）：默认 53.0s/2657 tokens → `enable_thinking=false`+`max_tokens=700` 为 1.5~1.7s；真实链路复测 ~1MB 图片：转述 10:21:26→10:21:28 约 2s，run 首字输出 7.4s（修复前用户实测 41.7s）；"请分析这张图片的内容"得到 332 字符转述与结构化分析回答 | 若去掉 `enable_thinking=false` 仅保留 `max_tokens`，对照实验为 23.0s（思考未被关闭），延迟回到两位数 | 通过 |
| 调优参数只发给 OpenAI 兼容系，不支持时退回朴素调用 | 同上 | `test_tuning_params_only_apply_to_openai_compatible_providers`（openai→带 `max_tokens`/`extra_body`；anthropic/gemini→两者都不带）；构造实验实测三种厂商客户端对 `extra_body` 的处理 | 若无条件带参，anthropic/gemini 的第一次转述请求会携带未知字段被供应商拒绝，用例中 `max_tokens not in kwargs` 的断言即失败 | 通过 |
| 转述文本里被抄错的路径在注入前自动纠正 | `yuxi/services/image_input_service.py` | `test_correct_transcript_paths_fixes_mistyped_hash`（`9fc2ac55…` 被抄成 `9fc2c554…` 后纠正回真实路径）、`test_transcribe_message_images_corrects_path_in_transcript`（端到端：转述含错路径 → 注入文本含正确路径） | `test_correct_transcript_paths_keeps_exact_and_skips_ambiguous`（完全匹配不动；歧义候选不纠正） | 通过 |
| `read_file` 抄错的路径在工具执行前自动纠正，歧义与其他工具不受影响 | `yuxi/agents/middlewares/model_input.py` | `test_corrects_mistyped_read_file_path_before_tool_execution`（实测缺陷回放：抄错 hash 的 read_file 被纠正为真实路径）、`test_keeps_exact_and_non_read_file_tool_calls_untouched` | `test_keeps_ambiguous_read_file_path_untouched`（歧义候选原样保留，避免改错模型本意）；若去掉歧义守卫该用例即失败 | 通过 |
| 声明不外露时，界面能力标记仍由静态快照给出且与实测一致 | `web/src/components/ModelSelectorComponent.vue`、`@opencode-ai/models/snapshot` | 撤回 `/models/v2` 字段与前端判定后，真实浏览器（Playwright，注入 token 打开 `/agent`）展开模型选择器：`qwen3.7-plus` / `qwen3.7-flash` 仍显示「支持图像输入」，`qwen3.7-max` 与 siliconflow 模型不显示；快照内容实测 `qwen3.7-max = ["text"]`、plus/flash 含 `image` | 若快照缺失该 provider，标记会全部消失——那时才需要让平台声明透出接口 | 通过 |
| 无回归 | `backend/test/unit/` | `docker exec test-api-1 sh -c "cd /app && python -m pytest test/unit -m 'not slow' -q"` → 2313 passed, 53 skipped（含新增 15 条）；`ruff check package`、`ruff format package --check`、`ruff check --select I package` 全部通过 | 无 | 通过 |

未覆盖：模型管理页保存「输入类型」后的端到端生效（保存与校验由既有 `input_modalities` 校验覆盖，本记录只验证读取与运行链路）；子智能体运行（`run_type=subagent`）不携带内联图片，走不到预路由分支。
