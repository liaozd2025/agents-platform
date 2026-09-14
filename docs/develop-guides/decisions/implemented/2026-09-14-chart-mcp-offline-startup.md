# 图表 MCP 使用镜像内的程序启动

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/agents/mcp/service.py

## 问题
内置图表 MCP 使用 npx 临时下载程序。生产网络访问 npm 失败时，每次工具初始化等待约一分钟，后续同线程请求因此排队。

## 决策
docker/api.Dockerfile 在构建期安装 @antv/mcp-server-chart 0.9.10，内置配置直接执行 mcp-server-chart。沿用现有内置配置同步，不新增运行时下载、重试或缓存机制。

## 替代方案
更换 npm 源仍依赖运行时网络；禁用图表工具会减少已有能力。选择预装已有依赖，将安装失败提前到镜像构建阶段。

## 后果
固定版本升级需要重建镜像。旧镜像与新启动配置不能混用，API 与 worker 必须一起更新。图表生成服务自身的外部网络请求仍需要可用网络。本次生产补丁以原运行镜像为基础，加入可信 npm 源下载的程序及依赖；保留原镜像与配置备份。

## 验证
- 容器内 test_mcp_service.py 与 test_mcp_router.py 共 22 项通过；运行配置回归断言在原 npx 配置下失败。
- 原镜像缺少本地程序；修复镜像在 network none、非 root 用户下完成真实 MCP initialize 与 tools/list，约 0.67 秒，返回 27 个工具。
- 生产 API 与 worker 就绪；真实 HTTP 聊天请求约 10.3 秒完成，返回预期文本；重新读取数据库确认 completed 和 output_message_id，随后通过 API 清理测试会话。
- 原先运行和排队的两个请求均完成。生产日志确认修复后的 MCP 在同一秒启动并加载 27 个工具。
- Ruff、工程契约检查与 61 项验证器单元测试通过。图表实际生成仍因外部服务连接重置失败，未宣称图表生成恢复。
