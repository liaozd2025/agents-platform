# 合并上游更新并保留 fork 契约

状态：implemented
类型：architecture
Owner：backend/package/yuxi/services/agent_request_service.py

## 问题

fork 与上游 main 从 v0.7.3 分叉。上游重组请求接入、资源选择和 Skill 服务，fork 同时拥有组织权限、知识库引用、用户资料、模型能力和持久化约束；直接覆盖任一侧都会丢失仍在使用的行为。

## 决策

将上游 `ca21a30d64a58269e7c5eefffd1afe24e0ad6f62` 合入当前本地工作分支，保留完整祖先关系。采用上游的新接口和服务分层，并在真实调用路径承接 fork 的权限和运行约束。

### 实现方案

HTTP 接入由 `agent_request_service.py` 协调，Message、Request 与按既有 FIFO 队头规则即时创建的 Run 在同一事务持久化，提交后才投递。聊天恢复同样在发布执行前提交事务。历史 PI 结果保持只读。Agent Context 使用上游的资源三态协议（空列表、固定列表、`all`），运行时仍通过 fork 的功能权限与资源可见范围求交；管理界面继续隐藏缺少功能权限的参数。

Skill 按 personal/shared/projection 服务分层：个人 Skill 自动参与运行，共享资源遵守配置选择和权限交集；个人启停状态、来源元数据、祖先目录符号链接拒绝和并发投影约束继续生效。内置 Skill 的管理权限由功能权限判断。异步子智能体采用 `subagent_start`、`subagent_status` 和 `subagent_await`；引用证据通过结果和 artifact 回传，保存本轮引用证据，正文和来源展示使用实际采用的引用。

`yuxi.storage.postgres.manager` 拥有业务 schema 版本 10：合并双方均使用版本 9 后发生的资源选择迁移，旧 schema 升级和 PI 退役按已有迁移阶段分别提交，资源配置改写与版本推进在锁与事务内原子完成。重复执行保留显式空列表，资源迁移失败时该阶段整体回滚。沙盒继续由 `SandboxResourceLimits` 统一拥有 Docker/Kubernetes/复用校验的预算，避免两套配置互相覆盖，见[资源上限决策](2026-09-23-sandbox-container-resource-limits.md)。

前端接入上游多图片、资源选择、Skill 安装与异步子任务交互，并保留 fork 权限、请求标识、引用和运行终态行为。图谱画布保留既有 Three.js 3D 实现，未用上游 G6 页面替换。测试和回放模型使用当前工具协议，Office 用例显式启用 OCR 工具，不恢复隐式注入。

## 替代方案

仅 fetch 不能使当前代码包含更新；整文件覆盖上游会丢失 fork 契约；逐提交摘取不利于后续识别上游祖先。选择三方合并，对冲突按实际接口和持久化语义整合。沙盒沿用既有统一限制，避免增加重复配置与优先级。

## 后果

业务 schema 升至 10，发布时必须通过现有离线迁移流程；本次仅更新本地代码，未迁移现有运行环境或生产数据。新增上游能力与 fork 权限共享同一条运行路径。未采用上游 G6 图谱界面和重复沙盒配置，不影响其余上游提交作为合并祖先存在。

## 验证

验证使用独立 Compose 项目及临时 PostgreSQL、Redis、MinIO、Milvus、API、worker 和 Docker sandbox，未复用生产数据。容器挂载当前源码，使用已有测试镜像，未重新构建发布镜像。

| 检查 | 命令或范围 | 结果 |
|---|---|---|
| 后端 unit | 容器内 `python -m pytest test/unit -m "not slow" --timeout=30 -q` | 2858 通过，59 跳过；引用工具名称参数化后专项 5 通过 |
| PostgreSQL schema | 真实数据库 schema 集成，含版本 8/9、事务回滚和重复空选择 | 26 通过 |
| HTTP 集成 | `test_agent_config_resource_authorization.py`、`test_dashboard_router.py`、`test_user_skill_projection.py` | 23 通过，含权限拒绝与独立数据库回读 |
| 主链路 E2E | `test_deterministic_agent_path_e2e.py` | 13 项整组通过；Office 显式启用 OCR 后单项通过，合计 14 项 |
| 引用 E2E | `test_answer_citations_e2e.py` | 5 通过，覆盖子任务、恢复、历史、采用来源和越权拒绝 |
| 前端 | `pnpm run lint:check`、`pnpm run test:unit`、`pnpm run build` | 通过，561 项 unit；构建仍有大 chunk 提示 |
| 工程检查 | `python3 -m unittest scripts.test_verify_engineering_contracts` | 64 通过 |
| 契约 gate | `python3 scripts/verify_engineering_contracts.py` | 仅既有 `AGENTS.md 5183 > 5000` 失败；HEAD 原文件同样超限，未降低规则 |
| 格式与依赖 | `git diff --check`、`uv lock --check`、变更 Python 文件 Ruff 检查 | 通过；`package/server/test` 范围保留 6 处既有 E501；含 `backend/scripts` 的全量 Ruff 共 30 处既有问题，相关文件均未改动 |

文档构建在最终决策落位后再次通过，相对链接检查无死链。源码经不继承实现上下文的独立 Reviewer 审查。真实浏览器登录页可渲染，后续表单交互被 Chrome 扩展界面阻挡；未完成登录后的页面、响应式和深色模式验收。

未运行远端 CI、完整集成/E2E 全集、真实 Kubernetes 或生产业务验收。既有知识库预检索范围问题不由本次合并引入，未在此扩展修复。
