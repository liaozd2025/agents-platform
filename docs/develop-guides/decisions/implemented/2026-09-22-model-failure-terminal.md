# 模型重试耗尽保持失败语义

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/agents/buildin/chatbot/graph.py

## 问题

模型请求持续返回错误时，ModelRetryMiddleware 的默认失败策略生成普通 AIMessage。主智能体和子智能体均使用该默认行为，导致 Run/Attempt 被保存为 completed，错误说明进入正常答案。

## 决策

ChatbotAgent 和 SubAgentBackend 的 ModelRetryMiddleware 显式设置 `on_failure="error"`。重试耗尽后原异常继续传播，经既有 BaseAgent、chat_service 和 worker 错误路径收敛为 failed。当前错误分类为 unexpected_error；错误消息绑定同一 Run 并带 is_error 标记，已生成内容按既有部分消息逻辑保存；SSE 返回错误和失败终态。

保留两处现有重试次数和退避策略，临时模型错误在重试后成功时仍正常完成。子 Run 失败后，父智能体仍可按既有 task 契约读取错误结果并继续处理；子失败不自动把父 Run 判为失败。

## 替代方案

按错误文案识别 AIMessage 依赖可变文本，可能误伤正常回答。新增中间件或 Run 状态会重复现有错误传播机制。直接设置库的失败策略只改两个真实装配点，继续使用已有终态事务、错误事件和清理机制。

## 后果

模型异常不再变成 completed 回答。错误正文继续使用现有错误字段和显示协议。本项不调整 Run 总超时预算、取消语义、队列行为或错误脱敏策略；真实模型供应商协议漂移及生产运行不在本次验证范围内。

## 验证

[真实 E2E](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/e2e/runs/test_model_failure.py) 覆盖主模型和通过 task 真实委派的子模型，各有正常响应、首次422后恢复、持续422三个场景。通过真实 HTTP 创建请求，worker 执行后独立回读 PostgreSQL、HTTP Run 状态、SSE 和模型服务端调用计数。

正常及恢复场景为 completed，最终消息分别归属对应 Run；持续失败场景 Run 和唯一 Attempt 均为 failed，错误字段存在，绑定的消息为空正文且带 is_error 标记，SSE 包含失败终态及合成错误。所有被检查 Run 均完成 runtime 清理。模型服务独立统计每种正常、恢复和耗尽场景分别调用一、二、三次，证明重试边界保持原行为。

同一测试在旧配置下准确报告主模型与子模型两次 `completed != failed`；只修改两处失败策略后，六场景全部通过。相关图装配与 worker 单测共75项通过。

```bash
bash backend/test/e2e/runs/run_model_failure.sh <本地依赖匹配的API镜像> <本地provisioner镜像>
docker compose exec -T api uv run --no-sync pytest test/unit/agents/test_summary_graph_config.py test/unit/services/test_run_worker.py -q
```

[一次性入口](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/e2e/runs/run_model_failure.sh) 复用隔离 Compose 应用拓扑，使用唯一 project、内部网络、临时目录及 PostgreSQL/MinIO tmpfs；测试成败均销毁本次容器、网络、卷和临时状态。测试模型只提供本地确定性 HTTP/SSE，不使用外部供应商或付费凭据。
