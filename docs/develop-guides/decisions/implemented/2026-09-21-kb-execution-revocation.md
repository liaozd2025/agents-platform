# 知识库工具在执行前重算可见范围

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/agents/toolkits/kbs/tools.py

## 问题

运行中的工具直接复用 Context 保存的可见知识库列表，共享撤销后仍可返回文件元数据。

## 决策

共用工具检查入口每次调用现有可见性 resolver，通过 KnowledgeBaseManager 和 repository 读取 PostgreSQL 当前用户组织与知识库共享配置，再与 Context.knowledges 求交集。七个知识库工具共用同一检查；空范围和查询异常沿用原有拒绝结果。权限查询失败不能退回旧列表。

## 替代方案

只刷新新 Run 无法约束运行中的旧对象。增加权限缓存失效广播会扩大实现，且仍需处理漏通知。删除缓存早返回直接复用现有授权查询，不新增权限模型、依赖或配置。

## 后果

每次工具执行增加现有用户和知识库查询，其中知识库列表查询会扫描全部知识库。共享撤销提交先于执行检查时生效；检查与内容读取不在同一事务中，不收回已经通过检查的操作、历史消息及已下载文件。功能角色撤销、账号锁定与删除的权限口径不属于本项共享范围修复。

## 验证

[工具 unit](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/unit/toolkits/test_kbs_tools.py) 通过七个公开工具入口验证同一 Context 撤权后的拒绝结果，并验证任务选库范围与权限源故障关闭。修复前列表工具、范围约束和权限源故障三个负向用例返回旧知识库而失败。

[真实运行 E2E](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/e2e/knowledge/test_execution_revocation.py) 在独立 HTTP API、ARQ worker、PostgreSQL 和 Redis 上运行。[确定性模型](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/e2e/knowledge/revocation_model.py) 在收到 search_file 工具定义后暂停；管理员通过真实 HTTP 提交撤权，PG 回读确认，新 HTTP 读取返回 404，再释放模型执行工具。修复前同一 Run 的最终消息仍显示观察到了私有文件；修复后 ToolCall 返回拒绝，同 Run 的最终 Message 不含文件名，唯一 Attempt 正常完成。恢复共享后同一 Context 能再次读取原文件元数据。

```bash
docker compose exec -T api uv run --group test pytest test/unit/toolkits/test_kbs_tools.py test/unit/backends/test_knowledge_base_backend.py test/unit/agents/test_context_auth.py -q
bash backend/test/e2e/knowledge/run_execution_revocation.sh <本地依赖匹配的API镜像> <本地provisioner镜像>
```

[一次性环境入口](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/e2e/knowledge/run_execution_revocation.sh) 使用专用 Compose 配置，生成独立 project，挂载当前源码并等待真实 readiness；EXIT trap 在成功或失败后删除本次容器、网络和 本次临时工作目录，PG/MinIO 使用 tmpfs。入口只复用本地镜像，内部网络不暴露宿主端口。测试创建合成管理员、用户、知识库和模型配置，已有用户时立即失败；只能通过该入口在一次性环境中运行，不能指向已有开发库或生产库。--confcutdir 排除不相关的全应用清理 fixture。

实验使用依赖匹配的已有镜像挂载当前源码，执行 uv run --no-sync；内部网络禁止访问外部供应商，未调用付费模型。覆盖的是共享撤销、文件元数据与运行结果，不声称完成外部向量库检索、文件二进制下载或生产容量验收。
