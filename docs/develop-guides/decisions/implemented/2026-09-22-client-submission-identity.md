# 客户端保留未确认发送的请求身份

状态：implemented
类型：bug-fix
Owner：web/src/components/AgentChatComponent.vue

## 问题

服务端提交成功而响应丢失时，Web 清除原发送状态，CLI 浏览器代理为重试生成新 request_id；再次发送会创建第二个 Request/Run。

## 决策

Web 对带 request_id 的普通创建请求使用原请求体重放一次。网络失败、408 和服务端错误属于结果未确认；明确的首次客户端拒绝不自动重试。重放仍失败时，当前聊天组件保留发送意图，显示恢复按钮并阻止作为新消息再次提交。恢复沿用原线程、模型、附件和编号；恢复按钮在请求处理中禁止重复触发。审批 resume 保留原有路径。

CLI 浏览器在发送前生成 UUID，本地代理验证并沿用该编号。结果未知时保留原始消息信封，恢复时继续使用首次 thread_id 字段，包括首次发送的 null，避免 Channel 会话来源改变。收到 Run 或排队 Request 的权威终态后清除待恢复状态；网络错误、非终态 EOF 和结果未知的服务端错误继续保留。Run SSE 的 DB/Redis 传输错误不视为业务终态，只有显式不可重试运行错误或 end 才结束发送意图。完成后用户主动再次发送相同文本会创建新编号。

后端 Request/Run 幂等约束继续拥有最终事实，客户端只保存发送身份，不增加去重数据库、正文指纹或服务端缓存。

## 替代方案

仅禁止双击不能覆盖响应丢失后的人工重试。靠消息文本去重会阻止用户有意重复发送。每次错误都换新编号会丢失与服务端已提交任务的关联，因此使用当前发送意图的原编号恢复。

## 后果

发送意图只保留在当前页面的聊天组件内存；刷新、关闭页面或销毁组件后的恢复不在本项范围。结果未知时先恢复原请求，不能直接开启同一意图的新任务。已失败的 Run 不会因恢复而重新执行。Web 接收创建响应后仍沿用现有 SSE 逻辑，本项不统一全部断线、排队 EOF 或审批恢复行为。

## 验证

[Web API 单测](https://github.com/liaozd2025/agents-platform/blob/main/web/test/unit/api_boundary.test.js) 验证丢响应后原编号与原请求体重放、明确拒绝及缺少编号不自动重试、连续失败保留未知结果。旧代码在首个丢响应场景直接失败，修复后通过。[CLI HTTP 测试](https://github.com/liaozd2025/agents-platform/blob/main/packages/yuxi-cli/tests/test_chat_web.py) 验证代理沿用编号、缺少合法编号时拒绝、网络未知与排队终态的区分；挂回旧代理后因擅自生成新编号而失败。

[浏览器 E2E](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/e2e/clients/browser.mjs) 启动真实 Web、CLI 页面、HTTP、ARQ worker 和 PostgreSQL，以确定性本地模型回答。只在真实服务已返回成功响应后注入丢包，覆盖 Web 自动及手动恢复、CLI 首次空线程、已收到 meta 后的流中断恢复，以及用户有意重复发送。页面回读恢复后的最终回答，随后数据库独立回读共六个 Request、六个 Run 和六个最终消息，四种恢复场景各一份，有意重复场景两份。Run completed、每个 Run 只有一个 Attempt、runtime_cleanup_pending=false。额外页面协议负向检查验证首次发送和恢复中的权威取消终态都会解锁输入。

```bash
docker compose exec -T -e PYTHONPATH=/repo/packages/yuxi-cli/src api uv run --no-sync pytest --confcutdir=/repo/packages/yuxi-cli /repo/packages/yuxi-cli/tests/test_chat_web.py /repo/packages/yuxi-cli/tests/test_client.py -q
cd web && pnpm run lint:check && pnpm run test:unit && pnpm run build
bash backend/test/e2e/clients/run_recovery.sh <本地依赖匹配的API镜像> <本地provisioner镜像>
```

CLI 测试需将仓库只读挂载到测试容器的 /repo，并将 CLI src 加入 PYTHONPATH；仓库现有 API 镜像包含上述两组测试所需依赖。[一次性入口](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/e2e/clients/run_recovery.sh) 需要本机 Node、Chrome、已安装的 Web 依赖和 packages/yuxi-cli/.venv。脚本复用隔离 Compose，使用唯一 project、临时数据库和合成账号，API 只向本机开放端口；测试结束销毁服务、网络、临时凭据和浏览器资料目录。没有访问生产或调用付费模型。
