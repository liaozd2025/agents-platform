# Yuxi 作为企业 AI 智能体中台底座的可行性分析

> **2026-08-13 实施更新**：本文关于“OA 提供 OIDC”的内容是前期假设，现已确认 OA 只有自定义双 JWT token 与 `GetUserInfo` 接口。S0 生产路径改为服务端校验 OA token 后交换 Yuxi token；OIDC 仅保留为可选身份源，其验签、nonce 与 Redis 多副本问题已修复。当前接入方式以 [OA iframe 接入说明](./oa-embed-integration.md) 和 [S0 实施设计](./2026-08-12-oa-embed-s0.md) 为准。

## Context

你要建企业内部智能助手，参考 Stripe 在 Deep Agents 上构建 Kai 知识 AI 平台的做法（安全、数据分离、Skill 知识库）。当前 `/Users/ddddup/Codebase/work/Yuxi` 是你选中的开源雏形（`xerrors/Yuxi`，MIT，0.7.1）。本文档回答三件事：**共通之处 / 是否可行 / 注意什么**。这一版只做分析，不含代码改动。

结论先给：**架构方向高度吻合，底座可用；但你最看重的"安全与数据分离"恰恰是这个项目当前最薄弱的一层，必须自建。**

---

## 一、Stripe Kai 的关键设计（文章提炼）

| 要点 | 内容 |
|---|---|
| 四层架构 | Deep Agents 基础层 → Stripe 内部 harness（安全/基础设施/内部服务）→ 配置层（各团队自建 agent 实例）→ Kai UI |
| Filesystem middleware | 云服务而非本地，**S3 支撑的虚拟文件系统**，"sync in / sync out"：执行前把相关文件落进沙盒，执行后同步回来 |
| Sandbox middleware | **Agent 跑在沙盒外，把沙盒当工具调用**——这是他们明确点名的安全边界，避免 LLM 生成代码带来的一类安全问题 |
| Summarization middleware | 长会话上下文衰减治理，阈值/模型/输出大小可调 |
| Skills 框架 | 500+ 内部 MCP 工具、1000+ skills；**联邦所有权**（各团队维护自己的 skill）；**两趟加载：先由 LLM 选 skill，skill 选择再 gate 工具上下文**；基础 skill 常驻（pinned）保证公司统一语境与策略 |
| 已知天花板 | 150+ skills 与 system prompt 组合时前沿模型出现质量衰减；正在做 RAG/分类器混合选择 |
| 设计信条 | "The whole promise of this product is you don't need to tune knobs."（不给终端用户暴露旋钮） |
| 未解问题 | 治理与合规护栏、行为个性化、多人协作会话 |

---

## 二、逐项对比：Kai vs Yuxi

### 强共通（可以直接复用，省下的是最贵的部分）

**1. 同一套技术底座 —— 不是"类似"，是同一个包**
`backend/package/pyproject.toml:22` 依赖 `deepagents>=0.6.7` + `langgraph>=1.0.1` + `langchain>=1.3.9`。Yuxi 的每个内置 agent 就是一次 `create_agent(...)` 加一串 middleware（`agents/buildin/chatbot/graph.py:79`），复用了 deepagents 的 `FilesystemMiddleware`、`CompositeBackend`、`SummarizationMiddleware`、`SKILLS_SYSTEM_PROMPT`。文章里 Stripe 说"Deep Agents 层解决所有非 Stripe 特有的问题"，Yuxi 用的是完全相同的那一层。

**2. 四层架构一一对应**

| Kai | Yuxi |
|---|---|
| Deep Agents 基础层 | `deepagents` + `langgraph` |
| Stripe harness | `backend/package/yuxi/agents/`（10 个自研 middleware + backends + toolkits）|
| 配置层 | `agents` 表 + `BaseContext` 字段元数据（`agents/context.py`，`kind`/`auth`/`configurable` 驱动前端表单）+ `share_config` |
| Kai UI | `web/`（Vue 3，~75k LOC）|

**3. 沙盒边界的设计完全一致 —— 这是最重要的一条**
Yuxi 的 agent 跑在 `worker-dev` 进程里，`execute` 是走 HTTP 打到独立 sandbox 容器的工具（`agents/backends/sandbox/backend.py:175`，`ProvisionerSandboxBackend`）。Agent 本体从不进沙盒。这跟文章里 "the agent runs outside the sandbox and calls into it as a tool, maintaining clean execution boundaries" 是同一个决策。你不需要重新设计这个边界。

**4. Skills 是 Anthropic Agent Skills 风格 + 渐进披露 + 工具门控**
`agents/middlewares/skills.py` 的三阶段：
- 预运行 `resolve_runtime_skills_for_context()` 载入可见 skills，DFS 展开 `skill_dependencies`（带环检测）
- prompt 只注入 `- **{name}**: {description}` + `-> Read /home/gem/skills/{slug}/SKILL.md`
- **模型 `read_file` 了某个 SKILL.md 才算激活**（`_process_tool_call_result`，路径严格 5 段校验），激活后才解锁该 skill 的 `tool_dependencies` 与 `mcp_dependencies`

这正是文章说的 "skill selection gates tool context" 的两趟机制，Yuxi 已经实现了，而且多了一层 skill 之间的依赖 DAG。存储：内容在 `saves/skills/<slug>/`，索引在 `skills` 表（`storage/postgres/models_business.py:229`，含 `tool_dependencies`/`mcp_dependencies`/`skill_dependencies`/`share_config`/`content_hash`/`version`）。运行时按线程物化到 `saves/threads/<skills_thread_id>/skills`，再**只读**挂进沙盒 `/home/gem/skills`。

**5. 内置 4 个 skill 已经示范了模式**：`image-gen` / `deep-research`（编排子智能体）/ `knowledge-base` / `mysql-reporter`。注意 **知识库检索本身就是一个 Skill 而非默认工具集** —— `category="knowledge"` 的工具要等 `knowledge-base` 的 SKILL.md 被读取才进模型可见列表。这个取舍和 Stripe 的 skill-gates-tools 一致。

**6. 上下文压缩比文章描述的更细**
`agents/middlewares/summary.py`（777 行）做两级：L1 把超限 ToolMessage 卸载到 `outputs/large_tool_results/` 只留路径+预览，L2 才真正摘要，且发 `context_compression` 流式事件给前端。阈值、保留条数、摘要 prompt、L2 触发比例都在 `BaseContext` 里可调。

**7. 子智能体比 deepagents 原生更进一步**
不是进程内子图，而是**出进程、ARQ 队列驱动的独立 run**（`services/subagent_run_service.py:102`），有自己的 `agent_runs` 行和 SSE 事件流，提供 `task` / `subagent_start` / `status` / `cancel` / `await` 完整异步生命周期，并且每次都用 `_get_verified_subagent_run(run_id, uid, created_by_run_id)` 校验归属。子智能体拿子 checkpoint 线程 + 父级 uploads/outputs + 自己的 skills 作用域。

**8. 评估已接 Langfuse**：`docs/agents/agent-evaluation.md` —— dataset/experiment/score 交给 Langfuse，Yuxi 只负责按真实 run 链路执行样例。这个边界划得对。

### Yuxi 额外有、文章没强调的

- **RAG + 知识图谱**：Milvus（一 KB 一 collection）+ Neo4j + MinerU/PaddleX/RapidOCR/DeepSeek-OCR 多解析器 + 分块策略 + 知识库评估。企业知识库这块 Yuxi 比文章描述的成熟。
- **LITE_MODE**：可以裁掉知识库/图谱/评估的重依赖，如果第一期只做 agent 中台，能省掉 Milvus/Neo4j/MinerU 一整套。

### 明确的差距

| 维度 | Kai | Yuxi | 影响 |
|---|---|---|---|
| 虚拟文件系统后端 | **S3** | 宿主机 `saves/threads/` bind-mount 进沙盒 | api/worker/provisioner **必须同机同盘**，无法水平扩展或上 K8s 多节点 |
| Skill 规模 | 1000+，两趟 + 正在做 RAG 选择 | 全量 name+description 进 prompt | Stripe 在 150 个就撞到质量衰减，你上量会撞同一堵墙 |
| MCP 工具规模 | 500+ 动态加载 | DB 注册表 + 按 skill 依赖加载 | 机制在，规模没验证 |
| 联邦所有权 | 各团队维护自己 skill | `share_config`(global/department/user) + `created_by` | 有雏形，**缺审核/签名/版本回滚流程** |
| pinned foundational skills | 有 | 无此概念（`workspace/agents/AGENTS.md` + system_prompt 近似） | 需自建 |
| 企业 harness（安全） | 文章的第二层核心 | **基本没有** | 见下节 |

---

## 三、可行性结论

**可行。** 判断依据：

- 你要的三样东西——Deep Agents 运行时、Skill 知识库、沙盒安全边界——**前两样已完整实现，第三样的架构决策与 Stripe 一致**。这部分自建约 6–12 人月，直接省了。
- MIT 许可（`LICENSE`），商用与闭源改造无障碍。
- 42k 行后端 + 75k 行前端 + 138 个测试文件，全 Docker Compose 化，有 `ARCHITECTURE.md` 代码地图和 `docs/agents/` 八篇设计文档，接手成本可控。

**但前提是你要认清：安全与数据分离这一层，Yuxi 现在等同于"演示级"，你是在自建，不是在配置。** 下面按阻塞程度排列。

---

## 四、必须注意的地方

### P0 —— 不改不能上生产

**1. 没有租户模型，知识库存在系统性越权**
`share_config` 三级（global/department/user）已定义并有评估函数，但**接收调用方传入 kb_id 的 46 条路由，没有一条调用 `check_accessible`**。它们只挂 `get_admin_user`，然后拿 `kb_id` 直接进 manager。举例 `knowledge_router.py:1537` 的 `/databases/{kb_id}/query`。

精确分布（补 S2 设计时逐行核实，以此为准）：

| 文件 | 条数 | kb_id 来源 |
|---|---|---|
| `knowledge_router.py` | 35 | 路径参数 `{kb_id}` |
| `knowledge_eval_router.py` | 8 | 路径参数 `{kb_id}` |
| `graph_router.py` | 3 | **Query 参数**（`/subgraph`、`/labels`、`/stats`）|

注意 `graph_router` 用 Query 而非路径参数，**一个统一的路径参数依赖覆盖不了它**。

另需修正早期表述：`check_accessible`（`knowledge/manager.py:236`）在整个 API 层**只有 1 个调用点** —— `workspace_router.py:38`，不是 5 个。列表类端点安全是因为走了另一条路（`get_databases_by_uid` → `_database_info_accessible`，`manager.py:301`），与 `check_accessible` 无关。

后果：**任意部门的 admin 只要知道 kb_id，就能查询/导出/删除任何部门的知识库。** 而 `admin` 在 Yuxi 里是全局角色（`ADMIN_ROLES = {"admin","superadmin"}`），不是部门管理员。

普通 `user` 这条路径是安全的（`get_databases_by_uid` → `resolve_visible_knowledge_bases_for_context` → 工具里再校验一次 `kb_id ∈ visible_kb_ids`，`toolkits/kbs/tools.py:191`），问题只在管理面。

**2. `departments` 不是隔离边界，只是共享受众**
除 `users` 和（从不被读取的）`api_keys` 外，**没有任何业务表带 `department_id`** —— `agents`/`skills`/`knowledge_bases`/`knowledge_files`/`conversations`/`agent_runs`/`mcp_servers`/`model_providers` 都没有。也没有 Postgres RLS（`create_business_tables()` 是纯 DDL）。隔离完全依赖 ~46 个手写调用点记得加过滤。

对话侧的 `uid` 行级隔离做得是对的（`services/conversation_service.py:105` 的 `require_user_conversation` 是唯一收口点，被 11 处正确调用），可以作为其他域的模板。

**3. 凭据明文落库且原样返回**
全仓库 grep `encrypt|Fernet|AES|cipher` 只命中一个 `redact_redis_url`。
- `model_providers.api_key`：明文 `String(500)`，`to_dict()` 原样返回（`models_business.py:672`），任意 admin 通过 GET/list 就能拿到全部模型厂商密钥，**无脱敏**
- `mcp_servers.env` / `headers`：明文 JSON，同样对 admin 全暴露（对普通用户已正确降级为 `{name, description, icon, enabled, tags}`）
- `agent_envs.env`：明文，整包注入沙盒容器

`api_key_env`（环境变量间接引用）是更安全的既有替代，内置 provider 用的就是它。

**4. 沙盒硬化几乎为零**
`docker/sandbox_provisioner/app.py:504` 起：`security_opt: ["seccomp=unconfined"]`、`tmpfs: /home/gem rw,exec,mode=777`、**没有** `cap_drop` / `no-new-privileges` / userns-remap / `read_only` rootfs / `mem_limit` / `cpu_quota` / `pids_limit`。
网络做对了一半：每个沙盒一张独立 bridge 网络（沙盒之间隔离、`app-network` 上的 postgres/milvus/minio/redis/neo4j 不可达），但 **没设 `internal=True`，出网完全不受限** ——无出口代理、无域名白名单、无 DNS 过滤。企业内网场景下这是数据外泄通道。
另外 provisioner 挂了 `/var/run/docker.sock`（`docker-compose.yml:137`），等于宿主机 root；它只用**一个平台级静态 bearer token** 鉴权，无按用户凭据。
已有的限制只有：exec 超时 180s、输出 256KB、空闲 120s。

**5. Skill 供应链风险**
`agents/skills/remote_install.py:112` 用 `asyncio.create_subprocess_exec` 在 **API/worker 容器内**跑 `npx -y skills add <source>`（只用临时 `HOME` 隔离），任意 `get_required_user` 都能经 `POST /api/skills/remote/prepare` 触发。第三方 skill 包先落到 API 容器文件系统，再只读挂进沙盒。**无审核、无签名、无版本回滚**。zip 导入那条路有 `_validate_zip_paths` 防 zip-slip，但 npx 这条没有等价防护。

**6. Token 无法吊销**
7 天 HS256 JWT，无 `jti`、无黑名单、无 refresh token。改密码、改角色、调部门、离职后旧 token 全部继续有效。API Key（`yxkey_` + SHA-256 存储）**继承持有者完整角色、无 scope**——admin 的 key 就是 admin key。
登录限流是进程内内存 `defaultdict[deque]`（10 次/60s），多 worker 失效，且盲信 `X-Forwarded-For`，只覆盖 `/api/auth/token`（OIDC 和 CLI token 端点无限流）。

**7. 知识库解析出的图片落在「公开可读」bucket**
`storage/minio/client.py:66`：
```python
PUBLIC_READ_BUCKETS = {"public"}
KB_BUCKETS = {"documents": "knowledgebases", "parsed": "knowledgebases", "images": "public"}
```
`ensure_bucket_exists()` 对 `public` 桶调用 `_ensure_public_read_access()`。也就是说**所有文档解析出来的图片都进了一个匿名可读的桶**，唯一保护是 `object_name = f"{prefix}/{uuid4()}.{ext}"` 的 UUID 混淆。roadmap 里已列为已知问题（"目前的知识库的图片存在公开访问风险"），在真多租户下这是 P0：跨租户的合同、报表、身份证件扫描件只要 URL 泄漏即全网可读，且无访问日志。

**8. 审计基本不存在**
只有一张 `operation_logs` 表，写入函数 `log_operation` **`except: pass` 吞掉所有异常**，全仓库 **14 个调用点**，全部集中在登录、用户 CRUD、部门 CRUD、OIDC 登录。

**没有任何审计记录的行为**：知识库检索/导出/删除、文档下载、Agent 运行、工具调用、MCP 与模型凭据变更、API key 增删、Skill 安装、沙盒创建与执行、会话访问。

且 **`operation_logs` 没有任何读取接口**——无查询 API、无导出、无保留策略、无防篡改。`superadmin` 的 `dashboard_router.py` 能看全站会话和完整对话记录，**而这个查看行为本身不被记录**。
`POST /api/auth/impersonate/{user_id}`（superadmin）只记录"授予"这一刻，签出的是普通 `{"sub": target_user.id}` token，**后续所有请求与真实用户无法区分**（无 `act` claim）。

### P1 —— 企业能力缺口

**8. 没有工具审批 / HITL 中间件**
全后端唯一的 `interrupt()` 在 `ask_user_question` 工具里（`toolkits/buildin/tools.py:383`）。**没有 `HumanInTheLoopMiddleware`，没有工具级审批门。** 一旦你的 agent 能写业务数据、调内部系统、发消息，这就是必建项。好消息：deepagents/LangChain 有现成的 HITL middleware，前端也已有 approval/HITL 相关 composable，接入成本不算高。

**9. OIDC 实现有硬伤**
`services/oidc_service.py`：生成了 `nonce` 也拿到了 `id_token`，但 **`id_token` 从不解码、从不验签**，身份完全来自 userinfo 端点；`state` 存在**进程内 dict**（类属性 `_state_store`，300s TTL），**多副本部署直接坏掉**。默认 `OIDC_AUTO_CREATE_USER=true`、默认角色 `user`、默认部门 `OIDC用户`。
无 LDAP/AD、无 SAML、无 MFA、无密码复杂度（仅 `min_length=8`）、无密码有效期。

**10. 存储不支持横向扩展**
Stripe 用 S3 做虚拟文件系统正是为了这个。Yuxi 的沙盒文件走宿主 `saves/threads/` bind-mount，**api / worker / sandbox-provisioner 必须在同一台机器共享同一块盘**。多副本或上 K8s 之前，必须把 `FilesystemBackend` 换成 MinIO/S3 实现。MinIO 已在编排里，改造有落点。

**11. Skill 选择会撞规模墙**
当前所有可见 skill 的 name+description 全量进 prompt。文章明说 Stripe 在 150 个 skill 时就观察到质量衰减。你如果规划到几百个 skill，需要提前做检索式/分类器式选择（Stripe 也还在做）。roadmap 里已有相关 issue：skill slug 全局唯一导致不同用户装同名 skill 冲突。

**12. Langfuse 数据出境**
默认指向 `https://cloud.langfuse.com`。traces 里的 prompt **包含检索到的知识库片段**。企业内部必须自托管 Langfuse，否则内部知识直接离开边界。目前 Langfuse 只做 LLM trace，不是审计（无完整性保证）。

**13. 图谱隔离弱于向量库**
Milvus 是**一 KB 一 collection**（`implementations/milvus.py:356`），物理隔离，很好。Neo4j 则是**同一个 database 内按 label 区分**（`safe_neo4j_label(kb_id)`，`storage/neo4j/manager.py:18`，只做正则校验防注入，不做隔离）。真多租户下这是最弱的一环 —— 任何能发 Cypher 的路径都跨越了 label 边界。注意：**Neo4j Community 版只支持单 database，per-tenant database 需要 Enterprise 授权**，内网离线部署要提前算这笔许可成本，或者接受 label 隔离 + 在服务层强制 label 白名单。

### P2 —— 工程与组织层面

**14. 上游是单人维护的开源项目**
`xerrors/Yuxi`，作者为在读博士生，README 里挂着求职信息，仓库自带赞助商板块。迭代活跃（0.7.1，roadmap 很密），但 **bus factor = 1**。这不否定项目质量（架构文档和分层比多数商业项目都清楚），但决定了你的策略：

> **建议：vendor fork + 定期 rebase，企业改动尽量走"新增文件 / 新增 middleware / 新增 dependency"，而不是改 `agents/`、`services/` 核心。**

具体讲，把企业改动收敛到三个"接缝"，rebase 成本最低：
- **一个统一授权模块**：`share_config` 的评估逻辑现在在 `repositories/agent_repository.py:139`、`agents/skills/service.py:146`、`knowledge/manager.py:208` **重复了三份形状相同的实现**。收敛成一个 authz 模块，再以 FastAPI 依赖的形式补到那 46 条 KB 路由上——这是新增，不是改写。
- **一个 FilesystemBackend 实现**：换 S3/MinIO，`backends/composite.py` 已经是 `CompositeBackend` 结构，是可插拔的。
- **一个审批 middleware**：新增文件挂进 `_build_middlewares`，不动 deepagents。

**15. 其他**：代码与文档以中文为主（团队需接受）；已在 `agents/models.py` 和 `backends/composite.py` 里内嵌了两处对上游 bug 的补丁（`_ToolCallChunkFixChatOpenAI`、`CustomCompositeBackend.glob`），说明上游还在成熟中；测试 138 个文件主要覆盖后端，前端偏薄；git 历史被压成 1 个 commit，无法从历史评估维护节奏，需去 GitHub 看。

---

## 五、已确认的四条前提

| 前提 | 选择 | 后果 |
|---|---|---|
| 隔离粒度 | **真多租户** | 需要 tenant 层 + 全表 `tenant_id`，是本项目最大的改动面 |
| 首期范围 | **知识库 + Agent 全量** | 不走 LITE_MODE，Milvus/Neo4j/MinerU/PaddleX 全上，运维面大 |
| 部署 | **内网离线，暂无硬性合规** | 出网限制反而变简单；但离线打破了若干现有假设（见下）|
| 上游 | **Vendor fork + 定期 rebase** | 改动要尽量"新增"而非"改核心" |

### 需要先解决的张力：真多租户 × Vendor fork

这两条天然打架。多租户意味着几乎每条仓储查询都要带 `tenant_id`，而逐条改 `repositories/`、`knowledge/manager.py`、46 条 KB 路由，正是 rebase 冲突最密集的地方 —— 走这条路，几个版本之后你就被迫硬分叉了。

**解法：把隔离下沉到数据库，而不是散在调用点。**

关键发现：全项目只有**一个** `async_sessionmaker`（`storage/postgres/manager.py:67`），`get_async_session()` / 上下文管理器都从它出。这意味着：

```
新增 migration：关键表加 tenant_id + ENABLE ROW LEVEL SECURITY + CREATE POLICY
改 1 处：session 工厂里注入 SET LOCAL app.current_tenant
新增 1 个 FastAPI 依赖：从 JWT 解析 tenant 并绑定到请求级 session
```

这样"改 46 个调用点"变成"新增 2 个文件 + 1 段 DDL + 动 1 个函数"，既拿到强制隔离（漏掉过滤时数据库直接返回空，而不是泄漏），又把 rebase 冲突面压到最小。**这是让你的两条前提同时成立的关键手法，建议在 spike 阶段就验证。**

配套要点：
- ARQ worker 和 LangGraph checkpoint 用的是独立连接池（`langgraph_pool`），RLS 策略要覆盖到，或显式豁免并单独审查。
- `conversations` / `agent_runs` 等已有的 `uid` 行级隔离（`services/conversation_service.py:105` 的 `require_user_conversation` 是唯一收口点）可以保留为第二道防线，不用拆。
- 三份重复的 `share_config` 评估逻辑（`agent_repository.py:139`、`skills/service.py:146`、`knowledge/manager.py:208`）收敛成一个 authz 模块，作为**租户内**的细粒度授权层 —— 租户隔离靠 RLS，租户内共享靠 share_config，两层职责分清。

### 内网离线打破的现有假设

- `agents/skills/remote_install.py` 走 `npx -y skills add` 拉 GitHub/ModelScope，**离线环境下直接不可用**。要么禁用该路由，要么改指向内部 npm registry / 内部 skill 仓库。顺带解决了前面 P0-5 的供应链风险 —— 离线是安全上的红利。
- 沙盒网络设 `internal=True` 变成零成本（本来也不需要出网），P0-4 的出网风险直接消失。
- Langfuse 必须自托管（本来内网也只能这样）。
- MinerU / PaddleX 的镜像与模型权重要预先拉取并入内网 registry；`docker-compose.yml:37` 的 `no_proxy` 列表需按内网拓扑重配。
- 内置模型供应商列表大部分指向公网 API，需要替换成内网推理服务（`models/providers/builtin.py` + `api_key_env` 间接引用是现成的落点）。

---

## 六、落地节奏（基于已确认前提）

| 阶段 | 目标 | 关键动作 | 验证方式 |
|---|---|---|---|
| **S0' OA 嵌入链路**（新的起点）| 拉通外部依赖 | 嵌入路由复用 `BlankLayout`；nginx 设 `frame-ancestors`；接 OA 的 OIDC 免登录；postMessage 三条消息（校验 origin，禁用 `*`）；**同时修 OIDC 两个硬伤**（`id_token` 验签、`state` 移 Redis）| 从模拟 OA 页面 iframe 进入，免登录发起一轮对话看到流式输出，全程无 Yuxi 登录框 |
| **S0'' 窄档布局** | 侧边栏/浮动可用 | 校准容器宽度阈值到 ~400px；file panel 接入同一套宽度判断；历史会话抽屉化；产物卡片点击请求 `fullscreen`；关掉 `AppLayout` 的 GitHub stars 请求（内网离线会一直失败重试）| 固定、浮窗与全屏模式下都能完成「提问→流式→产物→全屏保会话」闭环 |
| **S0 Spike**（1–2 周） | 验证底座 | 内网起全量环境；挂 2–3 个真实业务 skill（含一个连内部系统的 MCP）；跑通真实问答链路；**在 `conversations` 单表上做 RLS PoC**，确认 asyncpg 连接池 + `SET LOCAL` + ARQ worker + LangGraph checkpoint 池能跑通 | 一条真实业务问答端到端有引用来源；RLS PoC 下跨部门查询返回空 |
| **S1 部门隔离底座**（原「租户底座」，见第八节修正）| 隔离名副其实 | 关键表加 `owner_department_id` + RLS policy（变量 `app.current_department`）；session 工厂注入；部门来源取自 OIDC `department_claim`；Milvus collection 命名带部门前缀；Neo4j 定 label 白名单策略；MinIO **把 `images` 从 public 桶迁走**，改预签名 URL | 写一组跨部门越权测试（`backend/test/integration`），每条都必须 403/空 |
| **S2 安全底座** | 堵越权与凭据 | 统一 authz 模块 + 补齐 46 条 KB 路由；凭据应用层加密 + API 脱敏（`ModelProvider.to_dict()` 是关键点）；沙盒硬化（`internal=True`、`cap_drop`、去掉 `seccomp=unconfined`、资源限额）；token 吊销（`jti` + 黑名单）；禁用/改造 `remote_install` | 越权测试全绿；`docker inspect` 确认沙盒无出网、无多余 capability |
| **S3 治理** | 敢放开用 | 工具审批 middleware（新增文件挂进 `_build_middlewares`，前端已有 approval composable）；Skill 发布审核 + 版本 + 回滚；结构化审计覆盖 KB 访问 / agent run / 工具调用 / 导出下载，含查询接口与保留策略；自托管 Langfuse | 一次含审批的高危工具调用全链路可追溯 |
| **S4 规模化** | 上量 | FilesystemBackend 换 MinIO/S3（解开同机同盘约束）；skill 检索式选择（Stripe 的 150 墙）；多副本前先修 OIDC `state` 进程内存 与 登录限流进程内存 | 多副本下会话与 OIDC 登录正常 |

**三个"接缝"复述**（所有企业改动尽量收敛于此，保住 rebase 能力）：
1. 一个 authz 模块 + 一段 RLS DDL + 一个 session 依赖 → 隔离
2. 一个 `FilesystemBackend` 实现 → S3 化
3. 一个审批 middleware → 治理

---

## 七、追加需求：嵌入 OA（插件式、非侵入）

### 已确认前提

| 项 | 选择 |
|---|---|
| SSO 协议 | **标准 OIDC / OAuth2**，OA 当 Provider |
| 打通深度 | **仅身份**（免登录，权限仍在 Yuxi 内配）|
| 租户映射 | **一个 OA = 一个租户** |
| 部署域名 | **同主域子域**（如 `oa.corp.com` / `ai.corp.com`）|

### 现状对 iframe 嵌入意外地友好

| 事实 | 位置 | 为什么重要 |
|---|---|---|
| 认证走 `Authorization: Bearer` + localStorage，**不用 Cookie** | `stores/user.js:144`、`apis/base.js` | 完全绕开第三方 Cookie / SameSite / Safari ITP 一整类问题 |
| 流式走 fetch + `response.body.getReader()`，**不是 `EventSource`** | `composables/useAgentRunStream.js:35` | `EventSource` 无法设 `Authorization` 头，用它就被迫把 token 塞 query string（进 access log、经 Referer 泄漏）。现在的实现天然支持跨域带头流式 |
| CORS 已参数化，且含 `Last-Event-ID` | `main.py:44`、`EXPLICIT_CORS_HEADERS` | 跨域是配置项不是代码改动 |
| nginx 与 FastAPI 都没设 `X-Frame-Options`/CSP | `docker/nginx/default.conf` 全文无相关指令 | 今天就能被 iframe，无需拆 frame-busting |
| 已有 `BlankLayout` 无壳布局 | `router/index.js:13`（Home 在用）| 嵌入路由有现成复用点 |
| 已有 `/oidc/exchange-code` 端点形状 | `auth_router.py:1029` | 握手端点可照抄 |
| nginx 已配好 SSE 透传 | `default.conf`：`proxy_buffering off` + `proxy_read_timeout 600` | 长会话流式不会被代理截断 |

### 选型：iframe，不要微前端

iframe 是唯一真正"非侵入"的：OA 侧只加一个菜单项和一个 `<iframe>`，零框架依赖、零构建改动。

微前端（qiankun / wujie / micro-app）反而**是侵入的**——要求 OA 主应用引入并适配微前端框架，还要处理 Vite ESM 与 qiankun 的兼容、ant-design-vue 弹层 teleport 与样式隔离。除非 OA 本身已是微前端架构，否则不值。Web Component 同理。

### 必须处理的四件事

**1. `frame-ancestors` 白名单（安全 + 使能，同一个动作）**
现在任意站点都能 iframe 你的系统做点击劫持。加 `Content-Security-Policy: frame-ancestors 'self' https://oa.corp.com`，在 nginx `default.conf` 设即可。这既是启用嵌入，也是补上一个现存漏洞。

**2. 嵌入模式不能跳登录页**
`router/index.js:181` 未登录时 `return '/login'`，嵌入下用户会在 OA 页面里看到 Yuxi 登录框——体验破功，且**在 iframe 里训练用户输密码本身就是钓鱼训练**。嵌入模式下 guard 改为 `postMessage` 通知父窗口重新握手，不渲染任何登录 UI。

**3. iframe `sandbox` 属性**
OA 若给 iframe 加 `sandbox`，至少需要 `allow-scripts allow-same-origin allow-downloads allow-popups`，否则文件下载和产物预览会**静默失败**。这条要写进给 OA 团队的接入文档。

**4. 同主域子域是硬要求，不是优化**
选了同主域子域，localStorage 不被浏览器分区（Chrome 115+ / Safari 会按「顶层站点 + iframe 源」分区），握手态可复用。这条要写死进部署要求——一旦部署成跨主域或裸 IP，前面的免登录体验会退化成每次进 OA 都重新握手。

### 三种嵌入形态：对 Yuxi 是两个档位

OA 侧三种摆法：**侧边栏 / 全屏 / 右侧浮动**。侧边栏与浮动的差别（定位、圆角、阴影、能否拖拽）**完全在 OA 侧**，对 Yuxi 是同一个窄屏视图。所以是 2 个档位不是 3 个。

**使用混合边界，不使用 `?mode=` 参数。** 应用外壳根据 OA 父页确认的全局模式决定是否显示 PC 侧边栏；对话内的文件、状态和产物面板仍根据容器宽度响应。这样既保证全屏菜单的确定性，也不将对话布局写死在 400px / 1920px。

已确认的档位内容：

| | 窄档（侧边栏 / 浮动）| 宽档（全屏）|
|---|---|---|
| 对话主流程 | 保留 | 保留 |
| 历史会话 | **抽屉式** | 常驻侧栏 |
| 智能体切换 | 保留 | 保留 |
| 文件上传 | 保留 | 保留 |
| 产物预览 | **卡片 + 点击跳全屏（带会话）** | 停靠面板 |
| 知识库/图谱/仪表盘/扩展/评估 | 砍 | 保留 |

注意：窄档保留了全部对话能力，所以它**不是"精简版"，而是响应式重排** —— 工作量在布局而不在功能裁剪。

### 好消息：这套机制现有代码已经做了一半

`AgentChatComponent.vue` 里已经存在**由容器宽度驱动的面板停靠/悬浮切换**：

```js
// :780, :791  —— 判断依据是容器宽度，不是窗口宽度
const containerWidth = localUIState.chatContentWidth || getPanelContainerWidth()
// :794
const statePanelDocked   = computed(() => statePanelOpen.value && statePanelCanDock.value)
const statePanelFloating = computed(() => statePanelOpen.value && !statePanelDocked.value)
// :2037
localUIState.chatContentWidth = chatContentContainerRef.value?.clientWidth || localUIState.chatMainWidth
```

这正是窄档需要的机制，而且用的已经是 `clientWidth` 而非 `window.innerWidth`。`AgentArtifactsCard.vue` 也已经是卡片形态。所以窄档不是从零做，剩下的是四件事：

1. 校准现有阈值在 ~400px 下的行为
2. 把 file panel（现有独立的 `--file-panel-width` + 拖拽 resize）接到同一套容器宽度判断
3. 会话历史在窄档抽屉化（`AppLayout` 的常驻侧栏 → 抽屉）
4. 产物卡片在窄档改为 postMessage 跳全屏

### postMessage 协议（最小集）

| 方向 | 消息 | 用途 |
|---|---|---|
| OA → Yuxi | `{type:'oa:token', token}` | 下发/刷新登录态 |
| Yuxi → OA | `{type:'yuxi:auth-required'}` | 登录态失效，请求重新握手（替代跳登录页）|
| Yuxi → OA | `{type:'yuxi:mode-request', mode, threadId?}` | 请求固定、浮窗或全屏，带可选会话上下文 |
| OA → Yuxi | `{type:'oa:mode-changed', mode}` | 确认父页实际显示模式 |
| Yuxi → OA | `{type:'yuxi:close-request', threadId?}` | 请求隐藏助手，不销毁 iframe |

全屏侧按 `threadId` 定位**用现成路由即可**：`router/index.js` 已有 `/agent/:thread_id`（`AgentCompWithThreadId`），不用新建。

**安全红线**：`postMessage` 必须校验 `event.origin`，且 `targetOrigin` **绝不能用 `*`** —— 握手里传的是 token，用 `*` 等于把 token 广播给任何能 iframe 你的页面。这条和 `frame-ancestors` 白名单构成双重保险，两者都要有。

### OIDC 的两个已知硬伤，在这里从 P1 升为 P0

SSO 成为唯一入口后，`services/oidc_service.py` 的两个问题不再是"以后再说"：

1. **`id_token` 从不解码、从不验签** —— 身份完全来自 userinfo 端点。OA 是整个系统的信任根，信任根不验签等于没有信任根。
2. **`state` 存进程内存**（类属性 `_state_store`，300s TTL）—— 多副本直接坏；且 state 是 CSRF 防护，嵌入场景下更重要。改用 Redis（已在编排里）。

### 一个需要你注意的矛盾：「仅身份」与部门隔离

你选了「仅身份 SSO」（不同步组织架构），但 P0-1 要修的越权是**部门级**的，系统得知道用户属于哪个部门。而现在 `OIDC_AUTO_CREATE_USER=true`、默认部门写死 `"OIDC用户"` —— 所有 OA 用户会自动建号并全部落进同一个部门，部门隔离直接失效。

**建议**：用现有的 `department_claim` 配置从 OIDC claim 里取部门（`oidc_service.py` 已支持这个映射）。这只是读一个 claim，不构成"组织架构同步"，不违背你选的轻量路径，但让部门隔离有了数据来源。**如果 OA 的 OIDC 不吐部门 claim，就得退回到管理员在 Yuxi 里手工分配部门** —— 这个要提前跟 OA 团队确认。

---

## 八、基于「一个 OA = 一个租户」对前面方案的修正

**这条答案改变了 S1 的性价比判断，需要修正第六节。**

首期租户数恒为 1。此时全表 `tenant_id` + 以 tenant 为变量的 RLS **不提供任何实际防护**（所有数据同属一个租户），是为想象中的需求做建设。

但 RLS 这个**手法**依然值得用 —— 只是 policy 变量应该换成真正的隔离边界：

| | 原方案 | 修正后 |
|---|---|---|
| 隔离边界 | `tenant_id` | **`department_id`** |
| 首期防护价值 | 无（tenant 恒为 1）| **立刻生效**，直接堵住 P0-1 那 46 条路由的越权 |
| policy 变量 | `app.current_tenant` | `app.current_department` |
| 改动面 | 全表 | 知识库相关表为主 |

理由：RLS 的核心价值是"漏写过滤时数据库返回空而不是泄漏"，而 46 条手工补 authz 的路由正是最容易漏写的地方 —— 用 department 做 policy 变量，这个价值当场兑现，不用等到有第二个租户。

**做法**：
- 关键表加 `owner_department_id`，RLS policy 形如 `owner_department_id = current_setting('app.current_department')::int OR access_level = 'global'`
- `share_config` 里 `user_uids` 那种细粒度白名单仍留在应用层（RLS 做粗粒度兜底，authz 模块做细粒度）
- 多租户留作架构预留：schema 上可以留 `tenant_id` 列，但**不要为它设计**，等真有第二个法人再启用

第六节的 S0 PoC 目标随之调整：验证的是 `SET LOCAL app.current_department` 这条链路（asyncpg 连接池 + ARQ worker + LangGraph checkpoint 池），技术验证内容不变，只是 policy 变量换了。

---

## 九、下一步建议

分析到此完成，本文档不含任何代码改动。

四条 OA 前提全部指向轻量路径，整体工作量比第六节的原始排期显著下降：不做组织架构同步、不做权限同步、不处理存储分区、租户层退化为部门隔离。

**修正后的推荐起点：OA 嵌入链路（S0'）**，而不是原来的 RLS PoC。理由变了 ——「一个 OA = 一个租户」把 RLS 从"决定技术路线是否成立的高风险验证"降级成了"部门隔离的实现手法"，风险和不确定性都大幅下降；而 OA 嵌入现在是新的最高不确定性来源：它依赖 OA 团队的配合（OIDC 配置、claim 内容、iframe 接入），**外部依赖比技术风险更容易拖期**，应该最早开始拉通。

S0' 的最小验收标准：
1. 一条嵌入路由复用 `BlankLayout`，无侧边栏无顶栏
2. nginx 设 `frame-ancestors`，从一个模拟 OA 页面成功 iframe 进来
3. 走 OA 的 OIDC 完成免登录，能发起一轮对话并看到流式输出
4. token 过期时 `postMessage` 通知父窗口重新握手，**全程不出现 Yuxi 登录框**

**需要立刻找 OA 团队确认的三件事**（这是外部依赖，越早问越好）：
- OIDC 的 discovery 地址、client_id/secret 签发流程
- **userinfo / id_token 里是否带部门信息的 claim** —— 决定部门隔离有没有数据来源，没有就要退回手工分配
- iframe 接入方式：是否会加 `sandbox` 属性、能否配合 `allow-downloads`

**可以并行做的两件低成本项**（不依赖任何外部确认）：
- 修 OIDC 的两个硬伤（`id_token` 验签、`state` 移到 Redis）—— SSO 成为唯一入口后这是信任根
- 把 46 条 KB 路由的越权面写成一组**当前会失败**的集成测试（`backend/test/integration`），作为后续所有安全改动的验收基线

另外，动手前先建分支：仓库当前处于 **detached HEAD**（`## HEAD (no branch)`，且 `scripts/init.sh` 有未提交改动），此状态下任何提交都会丢。按 vendor fork 策略建议 `upstream` 指向 `xerrors/Yuxi`，企业改动落在 `enterprise/main`。
