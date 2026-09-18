# 移除 PI 执行扩展并采用上游子智能体方案

状态：implemented
类型：simplification
Owner：backend/package/yuxi/services/run_worker.py

## 问题

fork 在上游 Agent/SubAgent 与普通沙箱之外增加 PI 执行链，并将文件、命令和解析任务统一委派 PI child Run。该分叉扩大运行器、容器、协议与历史恢复的维护范围，也遮蔽了 v0.7.3 已有的后端 Docling Slim 解析入口。

## 决策

- 子智能体执行方案以 `upstream/v0.7.3`（`caff3208c9db9128aa0b277bc5c669141383ddbd`）为准，恢复普通 Agent/SubAgent 的文件、命令、解析工具与审批边界。
- 删除新 PI 任务入口、强制委派、Runner、PI 专属执行服务及镜像构建依赖；保留普通 sandbox-provisioner、Skill 投影、工作目录和通用资源限制。容量不足立即报告错误；没有执行消费者的 PI 异步等待 helper 和环境变量一并删除。
- 保留历史 PI 结果的查看与下载，停止旧 PI 任务续跑；只读兼容不提供新 PI 执行入口。business schema 9 在停机证明后将活动 PI、等待 PI 审批及已批准尚未完成的恢复任务收敛为失败，保留输入、结果、attempt 和文件。
- 旧 PI child 对话拒绝新增请求和恢复。父对话停在 PI 审批时，只退役该 PI 等待以允许新消息；普通审批与用户提问等待保持原语义。保留现有历史 attempt 列，避免移除 ORM 默认值影响普通 Run 插入。
- 保留后端 Docling Slim、旧 XLS 转换和 Office 预览所需的 LibreOffice；采用上游普通沙箱镜像默认值，不迁入另一分支的 Office 增量镜像。
- OA、组织权限、知识库、预览和普通子智能体通用界面修复等无关 fork 功能保留。当前范围为代码与隔离验证，不包含生产部署或清除用户数据。

术语见仓库 [CONTEXT.md](https://github.com/liaozd2025/agents-platform/blob/main/CONTEXT.md)，方向取代 [混合 PI 执行 ADR](../../../adr/0004-hybrid-pi-execution-seam.md)。[退役 ADR](../../../adr/0005-retire-pi-sandbox-delegation.md)只记录取舍，本记录拥有实施验收。

## 替代方案

- 保留 PI，仅恢复后端解析：仍维护两套执行方案，与用户删除 PI 的目标不符。
- 隐藏 PI 入口、保留新任务运行器：仍保留不需要的依赖和替代调用路径，不采用。
- 整文件覆盖所有上游差异：会丢失通用沙箱资源限制、历史结果和无关定制，不采用。
- 删除历史 PI 行与产物：不符合用户确认的保留要求，不采用。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 新任务按上游 Agent/SubAgent 执行 | 仅 UI 隐藏，仍可提交 PI | submission、worker、tool middleware | 2321 项 backend unit；61 项真实 PostgreSQL/HTTP；10 项 deterministic worker E2E | 显式提交 executor=pi 被拒绝；旧投递不会进入新执行器 | Passed |
| 主、子 Agent 能使用上游工具和审批 | PI 过滤或提示词残留 | composite、subagent graph、tool approval | 标准 AIO Sandbox 1.11.0 上的两种子任务模式、命令审批恢复与落盘回读 | 默认审批下子任务不能绕过执行限制；完全信任完成写入 | Passed |
| 历史结果可读，旧 PI 不续跑 | 历史 metadata 或 checkpoint 依赖运行器 | repository、chat_service、历史界面 | 真实 PostgreSQL、ASGI 路由、历史页面与文件字节回读 | 旧 child 新消息/resume、越权读取和下载被拒绝；待执行 PI resume 退役，普通审批保留 | Passed |
| Office 在后端解析并保留预览 | 错删有效 LibreOffice consumer | ocr_service、unified、filepreview | DOCX 后端解析 E2E；六种 Office 格式 HTTP 预览；锁文件及容器依赖回读 | parser 单测验证缺少 Calc 时旧 XLS 解析明确失败 | Passed |

旧能力不存在：源码装配、运行入口、Python 打包 manifest、Compose、Makefile 和 CI 均不再注册或构建 PI；Runner、专属服务与执行测试已删除。负向搜索只保留历史格式、退役 guard/迁移及相应验证。已删除 repository 执行方法在其余测试中没有消费者。

重新引入条件：有独立且已确认的业务需求，证明上游普通子智能体与沙箱无法满足，并重新评估依赖、历史兼容和真实链路证据。

## 后果

历史 PI child 没有普通子智能体 checkpoint，不能直接转换为可续跑的 SubAgent。普通沙箱与 API 后端是不同运行环境，API 安装 LibreOffice 不代表旧 Office Skill 在标准沙箱中具备全部编辑、重算和渲染依赖。已有数据库需要通过 `scripts/migrate-storage.sh` 停止旧 API、worker 和沙箱后前向迁移。预检兼容旧 thread/parent 列名；迁移幂等，知识域失败后仍可重试。不能把 schema 版本或历史约束直接降为上游值。生产部署、Kubernetes 和外部付费模型验收不在本次范围。

其他验证：Web lint、434 项 unit、build、真实页面浅色及 450px 深色历史详情通过；工程契约、对应 unittest、开发/生产 Compose 配置校验、Ruff 和 docs build 通过。旧列预检、已排队恢复漏退役均有先失败后通过的真实 PostgreSQL 回归。
