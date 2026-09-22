# 预览图片与数据库初始化日志的凭据边界

状态：implemented
类型：bug-fix
Owner：web/src/apis/knowledge_api.js

## 问题

Markdown 图片仅凭 URL 字符串含知识库图片路径就获得登录头，外站可借此接收凭据。PostgreSQL 初始化日志错误截取连接串，会输出密码。已确认的修复顺序见[最终裁决](https://github.com/liaozd2025/agents-platform/issues/104#issuecomment-5759628601)。

## 决策

知识库图片 API 在添加认证头前解析 URL，只对当前页面同源且规范化路径匹配知识库图片接口的地址发起认证请求，并限制 fetch 为同源模式。普通外站图片仍由浏览器加载，不附加平台认证。MarkdownPreview 复用该 API，保留已有 blob 显示和释放行为。

数据库初始化成功日志只报告初始化结果；失败日志保留异常类型，不记录连接串或可能包含连接串的异常正文。数据库日志行为由 `backend/package/yuxi/storage/postgres/manager.py` 拥有。此次不改变数据库连接、认证、迁移或失败状态语义。

## 替代方案

只在原正则上加前缀仍不能判断绝对 URL 的 origin。只依赖 CORS 不能阻止允许跨域的接收端取得登录头。把数据库密码用字符串替换遮盖会遗漏查询参数或异常中其他形式的秘密；不输出连接信息更简单。

## 后果

跨域重定向图片不再由认证 fetch 跟随；正常知识库图片由同源代理直接返回。普通外站图片仍可显示。异常日志减少连接细节，但保留异常类型和失败状态。所有验证使用合成凭据和隔离环境，未访问生产或付费模型。

## 验证

[图片 API 回归](https://github.com/liaozd2025/agents-platform/blob/main/web/test/unit/api_boundary.test.js) 验证绝对 URL、协议相对 URL、不同协议/端口、查询伪装与路径规范化；仅同源图片接口带认证并返回可显示的 Blob。[初始化日志回归](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/unit/storage/test_postgres_logging.py) 验证成功与异常两条路径均不输出合成密码或查询秘密。

真实 Chromium 加载 shipping MarkdownPreview 的隔离生产构建。原逻辑向外站发送三次带认证请求，修复后为零；同源认证图片完成 blob 替换且 naturalWidth 大于零。额外检查同源查询伪装及跨域重定向均未向不允许的目标发送认证。此探针验证组件、浏览器和真实本地 HTTP 接收端，不替代整个聊天路由或生产部署验收。

隔离 Compose 挂载当前源码，后端依赖声明/锁文件及前端 package/lock 与复用镜像一致。相关后端 31 项通过；完整后端 unit 为 2287 passed、53 skipped、7 subtests passed；前端 438 项 unit、lint 和生产构建通过。文档使用当前 frozen lock 离线安装依赖后构建通过。前端 unit 禁用开发依赖预打包以控制测试缓存，构建使用原始 Vite 配置。工程检查单测 62 项通过；总检查仍被既有登录页决策的 Owner 元数据格式错误阻断，本记录不宣称全仓 gate 全绿。

```bash
docker compose exec -T api uv run --group test pytest test/unit/storage/test_postgres_logging.py test/unit/storage/test_postgres_manager_schema.py test/unit/storage/test_langgraph_checkpointer_setup.py -q
docker compose exec -T api uv run --group test pytest test/unit -m "not slow"
docker compose exec -T web pnpm run lint:check
docker compose exec -T web pnpm run test:unit
docker compose exec -T web pnpm run build
python3 scripts/verify_engineering_contracts.py
python3 -m unittest scripts.test_verify_engineering_contracts
cd docs && corepack pnpm run build
```

以上是仓库标准入口；本次使用独立 Compose project，并在依赖一致的镜像中使用 `uv run --no-sync`，避免访问或重建现有发布环境。恢复原日志实现时两项断言因合成秘密进入日志而失败；恢复原图片处理时真实接收端捕获合成认证，浏览器断言失败。
