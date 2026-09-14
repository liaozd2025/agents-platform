---
name: image-gen
description: "通过 Kie 生成或编辑图片，支持 GPT Image 2 和 Nano Banana 2。当用户要求生成图片、海报、插画，或提供参考图要求修改时使用。"
---

# 图片生成技能

使用本技能的 `scripts/image_gen.py` 调用 Kie，在当前 Project Workdir 的 `outputs/` 保存图片，再调用 `present_artifacts` 展示。

## 模型与配置

- 默认模型：`gpt-image-2`。用户指定 Nano Banana 2 时使用 `nano-banana-2`。
- 两种模型都支持文生图和参考图编辑；首次仅支持这两个模型。后续模型在脚本的 `MODELS` 和参数处理处扩展。
- 必须从 **Agent 沙盒环境变量**读取 `KIE_API_KEY`。平台管理员可通过现有全局 `sandbox.env` 配置；用户也可在“沙盒环境变量”中覆盖同名变量。修改全局 `sandbox.env` 后，管理员需重启 `sandbox-provisioner` 再新建沙盒；用户环境变量更新只需新建沙盒。
- 密钥缺失时提示配置位置；禁止要求用户把密钥写进提示词、命令参数或脚本文件。
- 脚本使用沙盒已有的 Python、requests 和 Pillow。

## 执行流程

命令中的 `<技能目录>` 是本次读取的 `SKILL.md` 所在目录。脚本路径使用绝对路径，执行目录保持当前 Project Workdir，不能切换到只读技能目录。

1. 整理用户的图片内容、风格与约束；未指定尺寸时使用 `auto` 与 `1K`。
2. 若用户提供本地参考图，先逐张执行上传命令；只上传本次编辑需要的图片。文件必须位于当前沙盒的用户文件目录内，脚本拒绝符号链接、非图片和超过 30 MiB 的文件。

```bash
python <技能目录>/scripts/image_gen.py upload --file /home/gem/user-data/<参考图路径>
```

上传返回 `image_url`，仅用于下一步请求。用户提供的可公开访问的 HTTPS 图片地址可直接作为参考图；外部服务无法读取私有下载地址或宿主机路径。

3. 提交一次生成任务。参考图通过可重复的 `--image-url` 传入；省略该参数即为文生图。GPT Image 2 最多 16 张参考图，Nano Banana 2 最多 14 张。

```bash
python <技能目录>/scripts/image_gen.py generate --prompt '用户的图片需求'
python <技能目录>/scripts/image_gen.py generate --model nano-banana-2 --prompt '保留人物，改为水彩风格' --image-url 'https://example.com/reference.png' --aspect-ratio 16:9 --resolution 2K
```

以上是两种独立调用示例，每个用户请求按需要选择一条。返回 `state=submitted` 和 `task_id` 只代表任务已创建，此时不能声称图片已完成。

4. 使用返回的任务编号查询并下载。一次 `collect` 查询一次状态；若返回 `waiting`、`queuing` 或 `generating`，间隔至少 10 秒再查询同一个任务。总等待到 10 分钟仍未完成时，向用户报告任务仍在处理并保留任务编号，后续继续查询。

```bash
python <技能目录>/scripts/image_gen.py collect --task-id '<上一步的 task_id>'
```

5. 只有 `collect` 返回 `state=success` 与 `files`，才将 `files` 中的绝对路径交给 `present_artifacts`。这些路径指向当前 Workdir 的真实图片，文件后缀与内容格式一致。
6. 最终简要说明已生成的图片；临时图片 URL 和上传地址不作为最终交付物。

## 参数边界

`--resolution` 支持 `1K`、`2K`、`4K`；`--aspect-ratio` 按所选模型校验，可通过脚本中的 `MODELS` 查阅完整集合。

GPT Image 2：`auto` 只配合 `1K`；`2K` 不支持 `5:4`、`4:5`、`3:1`、`1:3`、`9:21`；`4K` 不支持 `1:1`、`3:1`、`1:3`、`9:21`。用户指定不支持的组合时说明限制，不静默修改要求。

## 失败与恢复

- `collect` 失败或超时：保留原任务编号并继续查询，不能重新执行 `generate` 冒充重试。已完成任务的下载失败也用 `collect` 重试。
- `generate` 返回 `submission_unknown` 或响应没有有效任务编号：创建结果不确定，先核对 Kie 任务记录，禁止自动重建付费任务。
- Kie 明确返回任务失败时报告失败原因。仅用户明确要求重新生成时才能再次提交。
- 脚本退出码非零表示错误；不要把残留文件作为本次成功结果展示。
- API Key 只发给 Kie 的生成、任务查询和上传接口；下载图片不携带该 Key，不自动跟随重定向。图片生成和下载均在沙盒内完成。

接口依据：[GPT Image 2](https://docs.kie.ai/market/gpt/gpt-image-2-text-to-image)、[GPT Image 2 编辑](https://docs.kie.ai/market/gpt/gpt-image-2-image-to-image)、[Nano Banana 2](https://docs.kie.ai/market/google/nanobanana2)、[文件上传](https://docs.kie.ai/file-upload-api/quickstart)、[任务查询](https://docs.kie.ai/market/common/get-task-detail)。
