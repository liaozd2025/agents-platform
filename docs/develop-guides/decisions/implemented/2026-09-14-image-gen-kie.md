# image-gen 接入 Kie 图片生成服务

状态：implemented
类型：feature
Owner：backend/package/yuxi/agents/skills/buildin/image-gen/scripts/image_gen.py

## 问题

image-gen 需要通过 Kie 提供 GPT Image 2 和 Nano Banana 2 的文生图与参考图编辑能力，并继续向用户交付当前工作目录内的图片文件。Kie 返回任务编号后仍需查询和下载；提交成功不能证明图片已生成。

## 决策

用户确认两个模型均支持文生图与参考图编辑，默认 GPT Image 2，采用平台统一 KIE_API_KEY。[技能脚本](https://github.com/liaozd2025/agents-platform/blob/main/backend/package/yuxi/agents/skills/buildin/image-gen/scripts/image_gen.py)拥有请求参数、异步任务查询和图片落盘；[技能说明](https://github.com/liaozd2025/agents-platform/blob/main/backend/package/yuxi/agents/skills/buildin/image-gen/SKILL.md)拥有 Agent 的调用流程；内置注册更新版本，使整个技能目录随现有同步机制分发。

脚本提供 upload、generate、collect 三个命令，分别上传参考图、创建任务、查询原任务并下载结果。每条命令执行一次对应请求；Agent 对待处理任务间隔至少 10 秒继续 collect，总等待达到 10 分钟时向用户交回原任务编号。生成请求不自动重试；提交超时明确报告结果未知，查询和下载失败继续使用原任务编号。

参考图片从沙盒用户目录读取，路径逐层禁止跟随符号链接；生成图片经解码验证后写入当前 Workdir 的 outputs，以实际格式生成后缀。下载请求不携带 Kie Key，也不自动跟随跳转。只有 collect 返回成功及文件列表，Agent 才通过现有 present_artifacts 登记产物；该工具继续拥有用户文件可见性与产物登记边界。

模型差异由小型参数映射及模型限制处理。后续增加模型需要同步增加参数适配和协议测试，不引入供应商框架或动态发现。全局密钥沿用现有 sandbox.env 配置方式；系统已有的用户同名环境变量覆盖能力保持有效，修改全局 sandbox.env 后需重启 sandbox-provisioner，使其重新读取文件，再新建沙盒；用户环境变量更新只需新建沙盒。

## 替代方案

- 只修改 Skill 中的请求示例：文件更少，但每次由 Agent 重新组织请求，不便集中验证失败、恢复和凭据边界。
- 技能附带小脚本：复用现有沙盒 requests 与 Pillow，集中验证两个模型的协议；采用此方案。
- 后端生成服务与回调入口：需要新增路由、状态和运维能力，当前没有对应需求。
- 单次长时间轮询：可能超过沙盒默认 180 秒执行限制。分开提交和查询可在后续调用中沿用原任务编号。

## 后果

内置技能限定为两个指定模型，原 Qwen 默认和任意供应商临时适配指引被替换；系统其它 SiliconFlow 模型配置不受此技能变更影响。

参考图按实际需要上传至 Kie，不能使用私有下载 URL 替代可公开读取的参考图地址。每个图片文件限制为 30 MiB，下载后的真实文件保留在用户工作目录；外部临时地址不是最终交付物。

脚本没有跨进程后台任务或回调服务。提交响应丢失时仍需核对 Kie 控制台；若收取多张图片时中途失败，已写入的文件可能保留，但错误结果不登记这些文件为交付物。再次 collect 下载原任务的结果。

## 验证

本地使用现有 API 镜像启动独立 Compose 测试容器，挂载本工作树，未修改运行中的开发服务或生产服务。测试 Key 为虚构值；外部协议测试使用本地 HTTP 替身。

- `uv run --no-sync --group test pytest test/unit/skills/test_image_gen_cli.py test/unit/services/test_skill_service.py`：通过。覆盖模型映射、参考图校验、失败与无效结果、任务编号、图片内容、文件路径、密钥缺失与脱敏。
- `uv run --no-sync --group test pytest --noconftest test/integration/test_image_gen_protocol.py`：4 项通过。真实 CLI 子进程经实际 HTTP 请求完成两个模型的文生图与编辑路径，断言仅创建一个任务、待处理状态、下载请求头以及最终文件字节。`--noconftest` 隔离无关的全局平台清理 fixture，此测试自行创建并清理 HTTP 服务和文件。
- `uv run --no-sync --group test pytest test/unit -m "not slow"`：1992 项通过、45 项跳过。隔离容器配置虚构 provisioner Token，并连接独立临时 PostgreSQL；初次因缺少这两项环境配置失败的 8 项均在补齐后通过。
- `uvx pyright --pythonpath /usr/local/bin/python3 --pythonversion 3.13 package/yuxi/agents/skills/buildin/image-gen/scripts/image_gen.py`：0 errors、0 warnings。变更 Python 文件的 `ruff check`、`ruff format --check` 与 `git diff --check` 通过。
- `python3 scripts/verify_engineering_contracts.py` 与 `python3 -m unittest scripts.test_verify_engineering_contracts`：通过，后者 61 项。`cd docs && pnpm run build`：完成；构建有 VitePress/Rolldown 兼容提示，生成的决策 HTML 已回读核验。
- 现有 PI 沙盒镜像中实际运行脚本的帮助及缺 Key 路径，分别返回帮助和结构化错误，依赖可导入。独立 Standards 与 Spec Review 完成，配置生效时机和凭据说明修正后无剩余阻塞。
- 真实 Kie 生成、真实 Agent 的 present_artifacts 登记及浏览器展示：Not run，等待测试 Key 和业务验收环境。CLI 协议替身测试不代替这些验收。

协议核验来源：[GPT Image 2 文生图](https://docs.kie.ai/market/gpt/gpt-image-2-text-to-image)、[GPT Image 2 编辑](https://docs.kie.ai/market/gpt/gpt-image-2-image-to-image)、[Nano Banana 2](https://docs.kie.ai/market/google/nanobanana2)、[文件上传](https://docs.kie.ai/file-upload-api/quickstart)、[任务查询](https://docs.kie.ai/market/common/get-task-detail)。上传文档的 OpenAPI server 字段与快速开始存在差异；使用快速开始与文件流上传示例共同列出的 kieai.redpandaai.co，实际可用性仍以真实 Key 测试为准。
